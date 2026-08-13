from __future__ import annotations
"""포트폴리오 & 실현손익 추적 모듈.

핵심 원칙: 매도하여 확정된 손익만 '수익'으로 인정.
보유 중인 종목의 평가손익은 '미실현(평가)손익'으로 별도 표기.

장기투자 종목도 별도 관리하여 20% 한도를 추적.
"""

import json
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR = "data"
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
LONG_TERM_FILE = os.path.join(DATA_DIR, "long_term.json")
HOLDINGS_SNAPSHOT_FILE = os.path.join(DATA_DIR, "holdings_snapshot.json")
EQUITY_CURVE_FILE = os.path.join(DATA_DIR, "equity_curve.json")

# 수수료율 — KIS 기준 보수적 추정. 실제 계약 수수료에 맞게 config에서 조정 가능.
# KR 매도는 거래세 0.18% + 농특세 0.15%(코스피의 경우 일부만) + 증권사 수수료. 보수적으로 0.2% 합산.
FEE_RATES = {
    "KR_BUY": 0.00015,
    "KR_SELL": 0.00015 + 0.0020,
    "US_BUY": 0.0025,
    "US_SELL": 0.0025 + 0.0000008,
}


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str) -> list | dict:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 찢어진 쓰기 자가 복구 — 비원자 writer 혼용/중단으로 "유효 JSON + 잔여 바이트"
        # 형태가 되면(equity_curve.json 'Extra data' 실측) 매 사용처가 크래시를 반복한다.
        # 유효 프리픽스만 파싱해 살리고, 원본은 .corrupt 로 보존한다.
        try:
            data, end = json.JSONDecoder().raw_decode(raw)
        except Exception:
            logger.error("JSON 복구 불가: %s — 원본을 .corrupt 로 보존, 빈 값으로 시작", path)
            try:
                os.replace(path, path + ".corrupt")
            except Exception:
                pass
            return []
        try:
            os.replace(path, path + ".corrupt")
            _save_json(path, data)
            logger.warning("JSON 자가 복구: %s — 유효 프리픽스 %d/%d바이트 (원본 .corrupt 보존)",
                           path, end, len(raw))
        except Exception:
            logger.warning("JSON 복구본 저장 실패: %s (메모리 복구본으로 계속)", path, exc_info=True)
        return data


_SAVE_LOCK = threading.Lock()


def _save_json(path: str, data):
    _ensure_dir()
    # 원자적 쓰기: trades.json 파손 = 실현손익·재진입가드 전체 손상.
    # 과거 .bak_* 수동 복구 파일들이 이 클래스의 증거. tmp + os.replace로 차단.
    # 락 + 스레드별 tmp 이름: EOD 락인 매도와 수동매매 동기화가 동시에 저장하면
    # 같은 .tmp를 서로 rename해 FileNotFoundError (라이브 07-16 15:06 실측).
    with _SAVE_LOCK:
        tmp = f"{path}.tmp.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


class PortfolioTracker:
    """실현손익 추적 + 장기투자 관리."""

    def __init__(self):
        _ensure_dir()
        self._trades: list[dict] = _load_json(TRADES_FILE)
        self._long_term: list[dict] = _load_json(LONG_TERM_FILE)

    # ── 매매 기록 ──

    @staticmethod
    def _market_meta(code: str) -> dict:
        # KR 종목코드는 6자리 숫자, 그 외(알파벳)는 US 티커로 판별
        is_us = bool(code) and not (code.isdigit() and len(code) == 6)
        return {"market": "US" if is_us else "KR", "ticker": code if is_us else ""}

    @staticmethod
    def estimate_fees(market: str, side: str, amount: float) -> float:
        """왕편 수수료+세금 추정. amount는 거래대금(avg 또는 current × qty)."""
        key = f"{market.upper()}_{'SELL' if side.lower().startswith('s') else 'BUY'}"
        return float(amount) * FEE_RATES.get(key, 0.003)

    # ── Equity Curve & 드로우다운 추적 ──

    def record_equity_snapshot(self, kr_cash: int, kr_eval: int,
                               us_cash_krw: int, us_eval_krw: int,
                               deposit_today: int = 0,
                               realized_today: int = 0,
                               fx_rate: float = 0.0,
                               us_cash_usd: float = 0.0,
                               us_eval_usd: float = 0.0,
                               unrealized_krw: int = 0,
                               total_override: int = 0,
                               holdings_unrealized_krw: int = None) -> dict:
        """일일 계좌 스냅샷 기록 + max_equity 대비 drawdown 계산.

        같은 날 재호출 시 마지막 값으로 갱신 (장중 다회 호출 대비).
        total_override 인자 추가 — 호출자가 net pending 포함한 정확한
        total을 넘기면 그 값으로 max_equity/drawdown 계산. 미정산 변동에 따라 max가
        잘못 부풀려져 dd가 가짜 -38% 표시되던 케이스 해결.
        holdings_unrealized_krw 추가 — 결제 타이밍에 영향받지 않는
        effective drawdown 산출용 (보유 종목 평가손익만; 현금/미결제와 무관).
        """
        decomposed = int(kr_cash + kr_eval + us_cash_krw + us_eval_krw)
        total = int(total_override) if total_override > 0 else decomposed
        curve = _load_json(EQUITY_CURVE_FILE)
        if not isinstance(curve, list):
            curve = []

        today = datetime.now().strftime("%Y-%m-%d")
        curve_prior = [c for c in curve if c.get("date") != today]
        max_equity = max([c.get("total_equity", 0) for c in curve_prior] + [total])
        drawdown_pct = ((total - max_equity) / max_equity * 100) if max_equity > 0 else 0.0

        snap = {
            "date": today,
            "kr_cash": kr_cash, "kr_eval": kr_eval,
            "us_cash_krw": us_cash_krw, "us_eval_krw": us_eval_krw,
            "total_equity": total,
            "deposit_today": deposit_today,
            "realized_today": realized_today,
            "max_equity": max_equity,
            "drawdown_pct": round(drawdown_pct, 2),
            "fx_rate": round(fx_rate, 2),
            "us_cash_usd": round(us_cash_usd, 2),
            "us_eval_usd": round(us_eval_usd, 2),
            "unrealized_krw": int(unrealized_krw),
        }

        #: settlement-immune effective drawdown.
        # total_equity(한투 tot_asst_amt)는 미국 T+2 미결제 매수 시 일시적으로 누락돼
        # 가짜 -15% drawdown을 만든다. 그 가짜값이 _drawdown_multiplier·adaptive crisis·
        # defensive 게이트를 동시에 트리거 → 자본의 75%가 묶이는 죽음의 나선을 유발했다.
        # effective_equity = 입금 + 누적실현손익 + 보유평가손익 (현금/결제 타이밍과 무관).
        if holdings_unrealized_krw is not None:
            realized_total = sum((t.get("realized_pnl") or 0) for t in self._trades
                                 if t.get("type") == "sell")
            deposits = self.get_total_deposits()
            eff = int(deposits + realized_total + int(holdings_unrealized_krw))
            prior_eff = [c.get("effective_equity") for c in curve_prior
                         if isinstance(c.get("effective_equity"), (int, float))]
            # 입금 시점(funding)에 계좌는 deposits 수준이었으므로 그것도 유효한 peak 기준.
            peak_candidates = prior_eff + [eff] + ([deposits] if deposits > 0 else [])
            max_eff = max(peak_candidates) if peak_candidates else eff
            eff_dd = ((eff - max_eff) / max_eff * 100) if max_eff > 0 else 0.0
            snap["effective_equity"] = eff
            snap["effective_drawdown_pct"] = round(eff_dd, 2)
            snap["effective_pnl_pct"] = (round((eff - deposits) / deposits * 100, 2)
                                         if deposits > 0 else 0.0)

        curve_prior.append(snap)
        _save_json(EQUITY_CURVE_FILE, curve_prior)
        return snap

    def get_total_fees(self) -> int:
        """trades.json 누적 매수+매도 수수료 합 (record_sell이 자동 저장하는 buy_fee/sell_fee 기반).

        record_sell 이전 거래는 fee 필드 없으므로 0으로 카운트. 이후 거래만 누적.
        """
        total = 0.0
        for t in self._trades:
            if t.get("type") == "sell":
                total += float(t.get("buy_fee") or 0)
                total += float(t.get("sell_fee") or 0)
        return int(round(total))

    def get_total_deposits(self) -> int:
        """누적 입금 추정.

        우선순위:
          1) data/total_deposits.json의 명시값 (사용자 수동 등록)
          2) equity_curve.json의 deposit_today 합
          3) equity_curve.json의 첫 날 total_equity (초기 자산을 입금으로 간주)
        """
        path = os.path.join(DATA_DIR, "total_deposits.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("manual_total_krw"):
                    return int(d["manual_total_krw"])
            except Exception:
                pass
        curve = _load_json(EQUITY_CURVE_FILE)
        if isinstance(curve, list) and curve:
            tracked = sum((c.get("deposit_today") or 0) for c in curve)
            if tracked > 0:
                return int(tracked)
            # fallback: 첫 스냅샷의 total_equity를 입금으로 간주
            sorted_curve = sorted(curve, key=lambda c: c.get("date", ""))
            return int(sorted_curve[0].get("total_equity", 0))
        return 0

    def get_effective_pnl_summary(self, total_equity_now: int,
                                  unrealized_now: int = 0) -> dict:
        """현 시점 기준 실효 순수익 분해 리포트.

        실현/미실현/환율효과/명목수익을 구분해 "진짜 번 돈 vs 표시상 증가" 시각화.

        Args:
            total_equity_now: 현재 총자산 (원화)
            unrealized_now: 현재 보유 종목의 평가차익 (원화 환산)

        Returns:
            {
              "realized_total": 누적 실현손익,
              "unrealized_krw": 현재 평가차익,
              "effective_total": 실현 + 미실현 (실효 순수익),
              "total_deposits": 누적 입금 (equity_curve 기반),
              "apparent_gain": 명목 증가액 (총자산 - 입금),
              "fx_and_other_effect": apparent_gain - effective_total,
            }
        """
        realized_total = sum((t.get("realized_pnl") or 0) for t in self._trades
                             if t.get("type") == "sell")
        total_deposits = self.get_total_deposits()
        total_fees = self.get_total_fees()

        apparent_gain = total_equity_now - total_deposits if total_deposits > 0 else 0
        effective_total = realized_total + unrealized_now
        # 환차익(또는 기타 잔차) = 명목 증가 − 실현 − 미실현
        # (수수료는 이미 realized_total에 차감 반영되어 있음 — record_sell 공식 참조)
        fx_other = apparent_gain - effective_total if total_deposits > 0 else 0

        return {
            "realized_total": realized_total,
            "unrealized_krw": unrealized_now,
            "effective_total": effective_total,
            "total_equity_now": total_equity_now,
            "total_deposits": total_deposits,
            "total_fees": total_fees,
            "apparent_gain": apparent_gain,
            "fx_and_other_effect": fx_other,
        }

    def get_current_drawdown(self) -> float:
        """현재 drawdown % (음수). 기록 없으면 0.0."""
        curve = _load_json(EQUITY_CURVE_FILE)
        if not isinstance(curve, list) or not curve:
            return 0.0
        latest = max(curve, key=lambda c: c.get("date", ""))
        return float(latest.get("drawdown_pct", 0.0))

    def get_effective_drawdown(self) -> float:
        """결제 타이밍에 영향받지 않는 effective drawdown % (음수, 위험 게이트용).

        total_equity 기반 get_current_drawdown()은 미국 T+2 미결제 매수 시 가짜로
        급락한다(예: 보유분 평형인데 -15% 표시). effective_equity(입금+실현+보유평가손익)
        기반 값을 우선 반환하고, 아직 기록이 없으면 기존 drawdown으로 폴백.
        """
        curve = _load_json(EQUITY_CURVE_FILE)
        if isinstance(curve, list) and curve:
            latest = max(curve, key=lambda c: c.get("date", ""))
            v = latest.get("effective_drawdown_pct")
            if v is not None:
                return float(v)
        return self.get_current_drawdown()

    def get_effective_pnl_pct(self) -> float:
        """입금 대비 effective 손익률 % (실현+미실현 기준, 결제 타이밍 무관).

        adaptive 상태의 pnl 트리거가 가짜 total_equity 대신 실제 손익을 보도록 한다.
        """
        curve = _load_json(EQUITY_CURVE_FILE)
        if isinstance(curve, list) and curve:
            latest = max(curve, key=lambda c: c.get("date", ""))
            v = latest.get("effective_pnl_pct")
            if v is not None:
                return float(v)
            # 폴백: total_equity 기반 (기존 동작)
            total = latest.get("total_equity", 0)
            deposits = self.get_total_deposits()
            if deposits > 0:
                return round((total - deposits) / deposits * 100, 2)
        return 0.0

    @staticmethod
    def _snap_equity(c: dict) -> int:
        """스냅샷의 effective_equity(결제타이밍 면역) 우선, 없으면 raw total_equity 폴백.

        total_equity(한투 tot_asst_amt)는 미국 T+2 미결제 시 일시 누락돼 가짜 -15~70% 낙폭을
        만든다. effective_equity = 입금 + 누적실현 + 보유평가 라 결제 타이밍과 무관.
        구 스냅샷(effective 필드 없음)은 raw 로 폴백해 하위호환.
        """
        v = c.get("effective_equity")
        return int(v) if isinstance(v, (int, float)) else int(c.get("total_equity", 0) or 0)

    @staticmethod
    def _snap_drawdown(c: dict) -> float:
        v = c.get("effective_drawdown_pct")
        return float(v) if isinstance(v, (int, float)) else float(c.get("drawdown_pct", 0.0) or 0.0)

    def get_monthly_stats(self, year: int, month: int) -> dict:
        """특정 월의 수익률·drawdown·입금액 집계 — effective(실효) 기준.

        결제타이밍 면역 effective_equity/effective_drawdown_pct 를 우선 사용해 T+2 팬텀
        가짜 낙폭을 제거한다(구 데이터는 raw 폴백). `basis` 로 어느 기준인지 표기.
        """
        curve = _load_json(EQUITY_CURVE_FILE)
        if not isinstance(curve, list):
            curve = []
        prefix = f"{year:04d}-{month:02d}"
        month_data = sorted([c for c in curve if c.get("date", "").startswith(prefix)],
                            key=lambda c: c["date"])
        if not month_data:
            return {"month": prefix, "days": 0}

        first = month_data[0]
        last = month_data[-1]
        tracked_deposits = sum(c.get("deposit_today", 0) or 0 for c in month_data)
        # 추적 안 된 입금 보정: deposit_today=0 인데 자산이 비정상 점프(전일의 2배↑ & 200만원↑,
        # 당일 실현손익으로 설명 안 됨)하면 입금으로 간주. 펀딩이 일별 deposit_today 없이
        # 누적 총입금으로만 기록되면 그 점프가 '수익'으로 잡혀 월 수익률이 +수천%로 왜곡되는데,
        # 그 입금분을 투입자본에 더해 정상화한다.
        inferred = 0
        prev = None
        for c in month_data:
            eq = self._snap_equity(c)
            if prev is not None and (c.get("deposit_today", 0) or 0) == 0:
                net_jump = eq - prev - (c.get("realized_today", 0) or 0)
                if net_jump > prev and net_jump > 2_000_000:
                    inferred += int(net_jump)
            prev = eq
        deposits = tracked_deposits + inferred
        start_equity = self._snap_equity(first)
        end_equity = self._snap_equity(last)
        invested = start_equity + deposits
        net_growth = end_equity - invested
        return_pct = (net_growth / invested * 100) if invested > 0 else 0.0
        max_dd = min((self._snap_drawdown(c) for c in month_data), default=0.0)
        # 이 달 스냅샷 중 하나라도 effective 필드가 있으면 실효 기준으로 본다.
        basis = ("effective" if any(isinstance(c.get("effective_equity"), (int, float))
                                    for c in month_data) else "raw")

        # 종목별 손익 (이 달 매도 집계) — 리포트의 "어떤 종목이 벌었나" 표
        from collections import defaultdict
        by = defaultdict(lambda: {"name": "", "code": "", "count": 0, "wins": 0, "pnl": 0})
        # 매도 패턴별 집계 (이 달) — 리포트의 "무엇이 돈을 벌었나" 표. 종합 리포트와 동일 축.
        bypat = defaultdict(lambda: {"pattern": "", "count": 0, "wins": 0, "pnl": 0})
        for t in self._trades:
            if t.get("type") != "sell" or not str(t.get("date", "")).startswith(prefix):
                continue
            code = t.get("code") or t.get("ticker") or "?"
            g = by[code]
            g["code"] = code
            g["name"] = t.get("name") or g["name"] or code
            g["count"] += 1
            p = t.get("realized_pnl") or 0
            g["pnl"] += p
            if p > 0:
                g["wins"] += 1
            pat = t.get("sell_pattern") or "other"
            gp = bypat[pat]
            gp["pattern"] = pat
            gp["count"] += 1
            gp["pnl"] += p
            if p > 0:
                gp["wins"] += 1
        by_stock = sorted(by.values(), key=lambda x: -x["pnl"])
        by_pattern = sorted(bypat.values(), key=lambda x: -x["pnl"])
        # 실현손익은 실제 매도 합(=종목별 표 합계)을 우선 — realized_today 가 비어도 정확.
        realized = (sum(g["pnl"] for g in by_stock) if by_stock
                    else sum(c.get("realized_today", 0) or 0 for c in month_data))

        return {
            "month": prefix,
            "days": len(month_data),
            "start_equity": start_equity,
            "end_equity": end_equity,
            "deposits": deposits,
            "realized": realized,
            "net_growth": net_growth,
            "return_pct": round(return_pct, 2),
            "max_drawdown": round(max_dd, 2),
            "basis": basis,
            "by_stock": by_stock,
            "by_pattern": by_pattern,
        }

    @staticmethod
    def _classify_sell_pattern(reason: str) -> str:
        """매도 reason 문자열을 구조화된 패턴 태그로 분류.

        반환 태그:
          split_profit     — 1차/2차 분할 익절 (승률 100% 패턴)
          rsi_overbought   — RSI 과매수 감지 (승률 100% 패턴)
          ambiguous_take   — 모호 구간(pop-then-fade) LLM 익절 타이브레이크
          trailing_stop    — 트레일링 스톱 발동
          breakeven_protect— 본전 보호
          forced_stop      — 손절선 (-15%) 도달
          crash_instant    — 급락 즉시 매도
          slow_bleed       — 느린 출혈 감지
          inverse_eod_lock — 인버스 마감 임박 수익 락인 (익일 개장 갭 전 실현)
          inverse_take     — 인버스 빠른 익절 (+1~2% 순익 즉시 실현 — 수익화 전략)
          inverse_exit     — 인버스 강제 청산
          rotate           — 종목 교체
          manual           — MTS/HTS 수동 매도
          other            — 분류 불가
        """
        r = (reason or "").lower()
        original = reason or ""

        if "모호판정" in original:
            return "ambiguous_take"
        if "익절" in original and ("1차" in original or "2차" in original or "3차" in original or "분할" in original):
            return "split_profit"
        if "rsi" in r and ("과매수" in original or "overbought" in r):
            return "rsi_overbought"
        if "트레일링" in original or "trailing" in r:
            return "trailing_stop"
        if "본전" in original or "breakeven" in r:
            return "breakeven_protect"
        if "손절" in original and ("강제" in original or "-15" in original):
            return "forced_stop"
        if "급락" in original or "crash" in r:
            return "crash_instant"
        if "출혈" in original or "bleed" in r:
            return "slow_bleed"
        if "인버스" in original and ("락인" in original or "eod" in r):
            return "inverse_eod_lock"
        if "인버스" in original and ("청산" in original or "exit" in r):
            return "inverse_exit"
        if "인버스" in original and "익절" in original:
            return "inverse_take"        # 인버스 빠른 익절(+1~2%) — 수익화 전략 측정용
        if "회전" in original or "rotate" in r or "갈아타" in original:
            return "rotate"
        if "수동" in original or "mts" in r or "hts" in r:
            return "manual"
        return "other"

    @staticmethod
    def _recent_bot_order(code: str, minutes: int = 15) -> bool:
        """최근 N분 내 봇 주문(pending_orders.json) 존재 여부 — 수동매도 오인 race 가드."""
        try:
            from datetime import timedelta as _td
            path = os.path.join("data", "pending_orders.json")
            if not os.path.exists(path):
                return False
            with open(path, encoding="utf-8") as f:
                orders = json.load(f).get("orders", [])
            cutoff = datetime.now() - _td(minutes=minutes)
            for o in orders:
                if o.get("code") != code:
                    continue
                try:
                    if datetime.fromisoformat(o.get("timestamp", "1970-01-01")) > cutoff:
                        return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def get_pattern_stats(self, days: int | None = None, *,
                          on_date: str | None = None, since: str | None = None,
                          market: str | None = None) -> dict:
        """매도 패턴별 성과 집계 (승률·건당 PnL·총합).

        Args:
            days: None이면 전체, 숫자면 최근 N일만 집계
            on_date: 지정 시 해당 날짜(YYYY-MM-DD) 매도만 집계 (days 무시)
            since: 지정 시 timestamp(ISO) ≥ since 매도만 집계 (US 세션은 KST 자정을
                   넘기므로 날짜 대신 시각 윈도로 직전 세션을 정확히 포착)
            market: 지정 시 해당 시장("KR"/"US") 매도만 집계 (market 미기록은 KR 취급)
        """
        from collections import defaultdict
        cutoff = None
        if on_date is None and since is None and days is not None:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        stats: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "wins": 0, "pnl_sum": 0, "amount_sum": 0}
        )
        for t in self._trades:
            if t.get("type") != "sell":
                continue
            if on_date is not None and t.get("date", "") != on_date:
                continue
            if since is not None and t.get("timestamp", "") < since:
                continue
            if cutoff and t.get("date", "") < cutoff:
                continue
            if market is not None and (t.get("market") or "KR") != market:
                continue
            pattern = t.get("sell_pattern") or self._classify_sell_pattern(t.get("reason", ""))
            pnl = t.get("realized_pnl") or 0
            amt = t.get("amount", 0) or 0
            stats[pattern]["count"] += 1
            if pnl > 0:
                stats[pattern]["wins"] += 1
            stats[pattern]["pnl_sum"] += pnl
            stats[pattern]["amount_sum"] += amt

        result = {}
        for pat, s in stats.items():
            # 가중 평균 수익률(%) — 금액 비례 가중. amount_sum=0이면 0.
            avg_pct = (s["pnl_sum"] / s["amount_sum"] * 100) if s["amount_sum"] else 0.0
            result[pat] = {
                "count": s["count"],
                "wins": s["wins"],
                "win_rate": (s["wins"] / s["count"] * 100) if s["count"] else 0.0,
                "pnl_sum": s["pnl_sum"],
                "avg_pnl": s["pnl_sum"] / s["count"] if s["count"] else 0,
                "avg_pct": avg_pct,
                "amount_sum": s["amount_sum"],
            }
        return result

    def get_fix_effect(self, baseline_date: str) -> dict:
        """수정 baseline(예) 전/후 매도 성과 비교 — 수정 효과 추적용.

        핵심: 조기손절(crash_instant/slow_bleed) 비중·손익이 수정 후 줄었는지,
        실현손익·승률이 개선됐는지. post 표본이 쌓일수록 hold-through·모델선택 효과가 드러남.
        """
        from collections import Counter
        sells = [t for t in self._trades if t.get("type") == "sell"]
        CUT = ("crash_instant", "slow_bleed", "rotate")

        def _agg(group: list) -> dict:
            n = len(group)
            pnl = sum((t.get("realized_pnl") or 0) for t in group)
            wins = sum(1 for t in group if (t.get("realized_pnl") or 0) > 0)
            cut_g = [t for t in group if t.get("sell_pattern") in CUT]
            rsi_g = [t for t in group if t.get("sell_pattern") == "rsi_overbought"]
            return {
                "n": n, "pnl": pnl, "wins": wins,
                "win_rate": (wins / n) if n else 0.0,
                "avg": (pnl / n) if n else 0.0,
                "cut_n": len(cut_g),
                "cut_pnl": sum((t.get("realized_pnl") or 0) for t in cut_g),
                "cut_share": (len(cut_g) / n) if n else 0.0,
                "rsi_n": len(rsi_g),
                "rsi_pnl": sum((t.get("realized_pnl") or 0) for t in rsi_g),
                "patterns": dict(Counter((t.get("sell_pattern") or "?") for t in group)),
            }

        pre = [t for t in sells if (t.get("date", "") < baseline_date)]
        post = [t for t in sells if (t.get("date", "") >= baseline_date)]
        return {"baseline": baseline_date, "pre": _agg(pre), "post": _agg(post)}

    def record_buy(self, code: str, name: str, qty: int, price: int,
                   is_long_term: bool = False, reason: str = "",
                   entry_context: str = "", entry_indicators: dict | None = None,
                   entry_analyst_details: dict | None = None):
        """매수 기록.

        ``entry_context``는 보상 엔진의 *진입 시점* 시장/셋업 라벨이다. 매도 때
        ``rsi_overbought`` 같은 청산 라벨을 진입 성과로 오인하지 않도록 체결과 함께
        보존한다. 구 호출부는 빈 값으로 호환된다.
        """
        record = {
            "type": "buy",
            "code": code,
            "name": name,
            "qty": qty,
            "price": price,
            "amount": qty * price,
            "is_long_term": is_long_term,
            "reason": reason,
            "entry_context": entry_context,
            "entry_indicators": entry_indicators or {},
            "entry_analyst_details": entry_analyst_details or {},
            "timestamp": datetime.now().isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            **self._market_meta(code),
        }
        self._trades.append(record)
        self._save_trades()

        if is_long_term:
            self._add_long_term(code, name, qty, price, reason)

        logger.info("매수 기록: %s %d주 × %s원 (장기: %s)", name, qty, f"{price:,}", is_long_term)
        from zusik.utils.logger import log_decision
        log_decision("BUY", name, code, qty, price, reason,
                     extra="장기보유" if is_long_term else "")

    def record_sell(self, code: str, name: str, qty: int, sell_price: int, avg_buy_price: int, reason: str = ""):
        """매도 기록 + 실현손익 계산."""
        market = self._market_meta(code)["market"]
        buy_amount = avg_buy_price * qty
        sell_amount = sell_price * qty
        buy_fee = self.estimate_fees(market, "buy", buy_amount)
        sell_fee = self.estimate_fees(market, "sell", sell_amount)
        realized_pnl = int(round(sell_amount - sell_fee - buy_amount - buy_fee))
        invested_amount = buy_amount + buy_fee
        realized_rate = (realized_pnl / invested_amount * 100) if invested_amount > 0 else 0

        record = {
            "type": "sell",
            "code": code,
            "name": name,
            "qty": qty,
            "price": sell_price,
            "amount": sell_amount,
            "avg_buy_price": avg_buy_price,
            "buy_fee": round(buy_fee, 2),
            "sell_fee": round(sell_fee, 2),
            "realized_pnl": realized_pnl,
            "realized_rate": round(realized_rate, 2),
            "reason": reason,
            "sell_pattern": self._classify_sell_pattern(reason),
            "timestamp": datetime.now().isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market": market,
            "ticker": code if market == "US" else "",
        }
        self._trades.append(record)
        self._save_trades()

        # 장기투자 목록에서 제거
        self._remove_long_term(code, qty)

        logger.info(
            "매도 기록: %s %d주 × %s원 | 실현손익: %s원 (%+.2f%%)",
            name, qty, f"{sell_price:,}", f"{realized_pnl:+,}", realized_rate,
        )
        from zusik.utils.logger import log_decision
        log_decision("SELL", name, code, qty, sell_price, reason,
                     extra=f"실현 {realized_pnl:+,}원 ({realized_rate:+.2f}%), 패턴 {record['sell_pattern']}")
        return {"realized_pnl": realized_pnl, "realized_rate": realized_rate}

    # ── 외부(수동) 매매 동기화 ──

    def reconcile_external_trades(self, current_holdings: list[dict],
                                   market: str = "KR", fx_rate: float = 1.0) -> int:
        """브로커 잔고와 마지막 스냅샷을 비교해 수동 매매(MTS/HTS) 자동 기록.

        Args:
            current_holdings:
              KR: [{"code", "name", "qty", "current_price", "avg_price"}, ...]
              US: [{"ticker", "name", "qty", "current_price", "avg_price"}, ...]
            market: "KR" | "US"
            fx_rate: US일 때 KRW 환산용 (KR이면 1.0)

        Returns:
            기록된 수동 매도 건수
        """
        # 마지막 스냅샷 로드
        if os.path.exists(HOLDINGS_SNAPSHOT_FILE):
            with open(HOLDINGS_SNAPSHOT_FILE, encoding="utf-8") as f:
                snapshot = json.load(f)
        else:
            snapshot = {"KR": {}, "US": {}}
        prev = snapshot.get(market, {})

        # 현재 잔고 → dict
        key_field = "code" if market == "KR" else "ticker"
        curr = {h[key_field]: h for h in current_holdings if h.get("qty", 0) > 0}

        manual_sells = 0
        carry_suspects: dict = {}  # 1차 감지 종목 — 이전 수량 그대로 이월해 다음 회차 재판정
        for code, prev_state in list(prev.items()):
            prev_qty = prev_state.get("qty", 0)
            if prev_qty <= 0:
                continue
            curr_state = curr.get(code)
            curr_qty = curr_state.get("qty", 0) if curr_state else 0

            if curr_qty < prev_qty:
                # 보유 감소 — 수동 매도 추정
                sold_qty = prev_qty - curr_qty
                avg_price_prev = prev_state.get("avg_price", 0)
                # 현재가가 가장 정확한 추정 (체결가는 알 수 없음)
                est_sell_price = (
                    curr_state.get("current_price") if curr_state else prev_state.get("current_price", avg_price_prev)
                ) or avg_price_prev
                # 봇이 같은 시각에 자동 매도 기록을 남겼는지 체크 → 중복 방지
                today = datetime.now().strftime("%Y-%m-%d")
                bot_sells_today = [
                    t for t in self._trades
                    if t.get("type") == "sell" and t.get("code") == code and t.get("date") == today
                ]
                bot_sold_qty = sum(t.get("qty", 0) for t in bot_sells_today)
                # 봇이 이미 기록한 수량 차감 후 남은 만큼만 수동 처리
                untracked_qty = sold_qty - bot_sold_qty
                if untracked_qty <= 0:
                    continue
                # race 가드: 주문 제출 → 잔고 감소 → record_sell 기록 사이에
                # sync가 끼면 '수동 매도'로 오인해 이중 계상 (KODEX 인버스 09:06 실측,
                # -160k 중복 → 일일손실한도 오발동). 최근 봇 주문이 있으면 이번 감지는
                # 봇 매도로 간주하고 보류. (race 윈도우의 진짜 수동 매도는 놓칠 수 있으나
                # 희귀하고, 이중 계상으로 한도 오발동하는 것보다 낫다.)
                if self._recent_bot_order(code, minutes=15):
                    logger.info("수동 매도 보류: %s — 최근 봇 주문 race로 판단, 기록 스킵", code)
                    continue

                # 2-패스 확정: 첫 감지에선 기록하지 않고 의심 마커만 남긴다.
                # 봇 매도의 주문→잔고감소→record_sell 사이에 동기화가 끼면 위 가드들이
                # 서브초 race로 뚫려 같은 매도가 '수동'으로 이중 계상됨(07-16 15:06 실측,
                # EOD 락인과 동시 기록 + 저장 충돌 크래시). 다음 동기화(수 분 뒤)까지
                # 봇 기록이 나타나면 위 untracked_qty 차감으로 자연 소거되고,
                # 그래도 남아 있으면 진짜 수동 매도로 확정 기록한다.
                if not prev_state.get("manual_suspect"):
                    carry_suspects[code] = {**prev_state,
                                            "manual_suspect": datetime.now().isoformat()}
                    logger.info("수동 매도 의심 1차 감지: %s %d주 — 다음 동기화에서 확정", code, sold_qty)
                    continue

                # KRW 환산 (US 종목)
                sell_krw = int(est_sell_price * fx_rate)
                avg_krw = int(avg_price_prev * fx_rate)
                name = prev_state.get("name", code)
                self.record_sell(
                    code, name, untracked_qty, sell_krw, avg_krw,
                    reason=f"수동 매도 (MTS/HTS, {market}, 체결가 추정)",
                )
                manual_sells += 1
                logger.warning("수동 매도 감지: %s %s %d주 @ %s (이전 %d주 → 현재 %d주)",
                               market, name, untracked_qty,
                               f"{sell_krw:,}원", prev_qty, curr_qty)

        # 새 스냅샷 저장 — 의심 종목은 이전 수량을 이월해 다음 회차에 감소를 재감지
        snapshot[market] = {
            h[key_field]: {
                "name": h.get("name", h[key_field]),
                "qty": h["qty"],
                "avg_price": h.get("avg_price", 0),
                "current_price": h.get("current_price", 0),
                "ts": datetime.now().isoformat(),
            }
            for h in current_holdings if h.get("qty", 0) > 0
        }
        snapshot[market].update(carry_suspects)
        os.makedirs(DATA_DIR, exist_ok=True)
        _save_json(HOLDINGS_SNAPSHOT_FILE, snapshot)

        return manual_sells

    # ── 실현손익 조회 ──

    def get_realized_pnl_today(self, market: str = "") -> dict:
        """오늘 실현손익 합계. market="KR"|"US"면 해당 시장 매도만 (손실한도 시장 분리용)."""
        today = datetime.now().strftime("%Y-%m-%d")
        sells = [t for t in self._trades if t["type"] == "sell" and t["date"] == today
                 and (not market or t.get("market") == market)]

        total_pnl = sum(t.get("realized_pnl", 0) for t in sells)
        total_sell_amount = sum(t.get("amount", 0) for t in sells)
        count = len(sells)

        return {
            "date": today,
            "realized_pnl": total_pnl,
            "sell_count": count,
            "sell_amount": total_sell_amount,
            "details": sells,
        }

    def get_realized_pnl_total(self) -> dict:
        """누적 실현손익."""
        sells = [t for t in self._trades if t["type"] == "sell"]
        total_pnl = sum(t.get("realized_pnl", 0) for t in sells)
        return {
            "total_realized_pnl": total_pnl,
            "total_sell_count": len(sells),
        }

    def get_trades_today(self) -> list[dict]:
        """오늘 매매 내역."""
        today = datetime.now().strftime("%Y-%m-%d")
        return [t for t in self._trades if t["date"] == today]

    def get_last_buy_time(self, code: str):
        """종목의 마지막 매수 시각 반환 (없으면 None)."""
        buys = [t for t in self._trades if t["type"] == "buy" and t["code"] == code]
        if not buys:
            return None
        last = buys[-1]
        try:
            return datetime.fromisoformat(last["timestamp"])
        except Exception:
            return None

    @staticmethod
    def classify_entry_bucket(reason: str) -> str:
        """매수 사유 → 진입 버킷. leftover/force_buy가 +EV인지 상시 관측용."""
        r = reason or ""
        if "수동" in r:
            return "manual"
        if "잔금" in r or "잔여" in r:
            return "leftover"
        if "강제매수" in r:
            return "force_buy"
        if "유휴" in r:
            return "idle_cash"
        return "normal"

    def get_entry_bucket_stats(self, days: int | None = None, month: str = "") -> dict:
        """진입 버킷별 실현손익 집계 — {bucket: {n, wins, pnl}}.

        매도는 같은 종목의 직전 매수 사유에 귀속(다중 매수 포지션은 근사치).
        entry_roi.py CLI와 월간 리포트가 공유. 시장 국면이 바뀌어 leftover가
        음수 전환하는지 운영자가 놓치지 않게 하는 관측 장치.
        month="YYYY-MM"이면 해당 월 매도만 — 월간 결산과 같은 기간축."""
        cutoff = ""
        if days:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        last_buy: dict = {}
        stats: dict = {}
        for t in self._trades:
            code = t.get("code")
            if t.get("type") == "buy":
                last_buy[code] = self.classify_entry_bucket(t.get("reason"))
            elif t.get("type") == "sell":
                if cutoff and str(t.get("date", "")) < cutoff:
                    continue
                if month and not str(t.get("date", "")).startswith(month):
                    continue
                b = last_buy.get(code, "unknown")
                pnl = t.get("realized_pnl", t.get("pnl", 0)) or 0
                s = stats.setdefault(b, {"n": 0, "wins": 0, "pnl": 0})
                s["n"] += 1
                s["wins"] += 1 if pnl > 0 else 0
                s["pnl"] += pnl
        return stats

    def get_last_buy_reason(self, code: str) -> str:
        """종목의 마지막 매수 사유 문자열 (없으면 ""). 다중 매수 포지션은 마지막 매수
        기준이라 근사치 — 수동/자동 구분(학습 제외) 용도로는 충분."""
        for t in reversed(self._trades):
            if t.get("type") == "buy" and t.get("code") == code:
                return str(t.get("reason") or "")
        return ""

    def get_last_buy_context(self, code: str) -> str:
        """종목의 마지막 매수에 저장된 진입 컨텍스트 반환 (구 기록은 빈 문자열)."""
        for t in reversed(self._trades):
            if t.get("type") == "buy" and t.get("code") == code:
                return str(t.get("entry_context") or "")
        return ""

    def get_last_buy_indicators(self, code: str) -> dict:
        """마지막 매수 당시 지표. 구 기록/잔금 주문은 빈 dict."""
        for t in reversed(self._trades):
            if t.get("type") == "buy" and t.get("code") == code:
                v = t.get("entry_indicators")
                return dict(v) if isinstance(v, dict) else {}
        return {}

    def get_last_buy_analyst_details(self, code: str) -> dict:
        """마지막 매수 판단에 참여한 애널리스트별 신호."""
        for t in reversed(self._trades):
            if t.get("type") == "buy" and t.get("code") == code:
                v = t.get("entry_analyst_details")
                return dict(v) if isinstance(v, dict) else {}
        return {}

    # ── 장기투자 관리 ──

    def _add_long_term(self, code: str, name: str, qty: int, price: int, reason: str):
        existing = next((lt for lt in self._long_term if lt["code"] == code), None)
        if existing:
            # 평균 단가 재계산
            total_qty = existing["qty"] + qty
            total_cost = existing["qty"] * existing["avg_price"] + qty * price
            existing["avg_price"] = total_cost // total_qty
            existing["qty"] = total_qty
            existing["reason"] = reason or existing["reason"]
            existing["updated"] = datetime.now().isoformat()
        else:
            self._long_term.append({
                "code": code,
                "name": name,
                "qty": qty,
                "avg_price": price,
                "reason": reason,
                "added": datetime.now().isoformat(),
                "updated": datetime.now().isoformat(),
            })
        self._save_long_term()

    def _remove_long_term(self, code: str, qty: int):
        lt = next((x for x in self._long_term if x["code"] == code), None)
        if not lt:
            return
        lt["qty"] -= qty
        if lt["qty"] <= 0:
            self._long_term = [x for x in self._long_term if x["code"] != code]
        self._save_long_term()

    def get_long_term_holdings(self) -> list[dict]:
        """장기투자 종목 목록."""
        return list(self._long_term)

    def get_long_term_total_cost(self) -> int:
        """장기투자 총 투입 금액."""
        return sum(lt["qty"] * lt["avg_price"] for lt in self._long_term)

    def is_long_term(self, code: str) -> bool:
        """해당 종목이 장기투자인지 확인."""
        return any(lt["code"] == code and lt["qty"] > 0 for lt in self._long_term)

    # ── 저장 ──

    def _save_trades(self):
        _save_json(TRADES_FILE, self._trades)

    def _save_long_term(self):
        _save_json(LONG_TERM_FILE, self._long_term)
