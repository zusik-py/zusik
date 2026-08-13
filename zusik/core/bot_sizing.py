from __future__ import annotations

import logging


from zusik.core.risk_manager import RiskManager
from zusik.core.position_manager import PositionManager


logger = logging.getLogger(__name__)


class SizingModeMixin:
    """SizingModeMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _apply_mode_change(self, new_mode: str):
        """모드 전환 시 종목 수/선별 스타일 반영."""
        from zusik.core.trading_mode import MODE_PROFILES, determine_auto_mode, _load_state, _save_state

        old_mode = self._active_mode
        self._active_mode = new_mode
        MODE_PROFILES.get(new_mode, {})

        # 종목 수 조정
        try:
            balance = self.client.get_balance()
            total_asset = balance["cash"] + balance["total_eval"]
            tier = determine_auto_mode(total_asset)
            self.config.setdefault("screening", {})["kr_count"] = tier["kr_count"]
            self.config.setdefault("screening", {})["us_count"] = tier["us_count"]
        except Exception:
            pass

        # 포지션 관리 파라미터 갱신
        self.positions = PositionManager(self.config)
        self.risk = RiskManager(self.config)

        state = _load_state()
        state["current_mode"] = new_mode
        _save_state(state)

        logger.info("모드 전환 완료: %s → %s", old_mode.upper(), new_mode.upper())

    @staticmethod
    def _kelly_fraction(mc: dict) -> float:
        from zusik.analysis.bot_money_helpers import compute_kelly_fraction
        return compute_kelly_fraction(mc)

    def _entry_quality_gate(self, df, *, is_inverse: bool = False,
                            is_add_on: bool = False) -> tuple[bool, str]:
        """신규 진입의 로컬 품질 게이트.

        청산 패턴이 매수 통계로 섞였던 기간에 RSI 84.9 같은 극단 과열 종목까지
        ``97.3% 승률``로 추격했다. LLM 판단과 무관하게 신규 진입은 양의 모멘텀을
        요구하고, 과열 구간은 거래량이 동반된 실제 돌파일 때만 제한적으로 허용한다.
        인버스와 이미 수익 중인 피라미딩은 각자의 전용 규율을 사용한다.
        """
        cfg = self.config.get("entry_quality", {}) or {}
        # 구형 임베더/테스트 config에는 키가 없다. 명시 활성일 때만 새 정책 적용.
        if not cfg.get("enabled", False) or is_inverse or is_add_on:
            return True, "전용 진입 규율"
        if df is None or len(df) < 21:
            return False, "OHLCV 21봉 미만"
        try:
            from zusik.analysis.indicators import (
                breakout_signal, calc_rsi, momentum_score, volume_surge,
            )
            rsi = float(calc_rsi(df).iloc[-1])
            momentum = float(momentum_score(df))
            min_momentum = float(cfg.get("min_momentum", 0.08))
            hard_rsi = float(cfg.get("hard_rsi_max", 82.0))
            warm_rsi = float(cfg.get("breakout_required_rsi", 74.0))
            breakout_momentum = float(cfg.get("breakout_min_momentum", 0.25))
            volume_ratio = float(cfg.get("breakout_min_volume_ratio", 1.2))

            if rsi != rsi or momentum != momentum:  # NaN
                return False, "지표 NaN"
            if rsi >= hard_rsi:
                return False, f"RSI {rsi:.1f} ≥ {hard_rsi:.0f} 극단 과열"
            if momentum < min_momentum:
                return False, f"모멘텀 {momentum:+.2f} < {min_momentum:.2f}"
            if rsi >= warm_rsi:
                breakout = breakout_signal(df, 20)
                volume = volume_surge(df, 20, volume_ratio)
                if (not breakout.get("is_breakout")
                        or not volume.get("is_surge")
                        or momentum < breakout_momentum):
                    return False, (
                        f"RSI {rsi:.1f} 과열: 돌파={bool(breakout.get('is_breakout'))}, "
                        f"거래량={float(volume.get('ratio', 0)):.1f}배, "
                        f"모멘텀={momentum:+.2f}"
                    )
            return True, f"RSI {rsi:.1f}, 모멘텀 {momentum:+.2f}"
        except Exception as e:
            return False, f"진입 품질 계산 실패: {e}"

    def _cash_deployment_ratio(self, ratio: float, cash_ratio: float,
                               confidence: float, symbol: str = "") -> tuple[float, str]:
        """고품질 BUY에 한해 초과 현금을 더 빠르게 투입한다.

        신호를 완화하거나 잔금을 아무 종목에 넣지 않는다. 확신도 하한을 넘긴 정상 BUY의
        사이즈만 현금비중에 비례해 키우며, 기존 adaptive 종목 상한은 그대로 지킨다.
        """
        cfg = self.config.get("cash_deployment", {}) or {}
        if not cfg.get("enabled", False):
            return ratio, "현금 집행 OFF"
        min_conf = float(cfg.get("min_confidence", 0.65))
        target = float(cfg.get("target_cash_ratio", self.config.get("_cash_reserve", 0.05)))
        full_at = max(float(cfg.get("full_boost_cash_ratio", 0.20)), target + 0.01)
        max_boost = max(1.0, float(cfg.get("max_size_boost", 1.5)))
        if confidence < min_conf or cash_ratio <= target:
            return ratio, "현금 집행 조건 미달"

        progress = max(0.0, min(1.0, (cash_ratio - target) / (full_at - target)))
        boost = 1.0 + (max_boost - 1.0) * progress
        adapt = self._adaptive_params()
        if symbol and self._is_whitelist(symbol):
            cap = float(adapt.get("whitelist_cap", adapt.get("cap", 0.0)) or 0.0)
        else:
            cap = float(adapt.get("cap", self.config.get("invest_ratio_max", 0.0)) or 0.0)
        adjusted = ratio * boost
        if cap > 0:
            adjusted = min(adjusted, cap)
        return adjusted, (
            f"초과현금 {cash_ratio:.0%}>{target:.0%}, 확신 {confidence:.0%} "
            f"→ ×{boost:.2f} (상한 {cap:.0%})"
        )

    def _rsi_trim_ratio(self, symbol: str) -> float | None:
        """RSI 과매수 익절 분할 트림: 에피소드당 1회 절반 익절, 나머지 라이딩.

        근거: 사후가격 검증 익절 적시성 27% — 전량 즉시 익절이 CSCO +26%, 삼성전기 +30%,
        KTOP +12% 후속 상승을 놓침. 절반은 즉시 익절(100% 승률 패턴 유지), 절반은
        트레일링(0.15/+7%)·본전보호가 관리하는 라이딩으로 상방만 연다.

        24h 내 재트리거(RSI가 계속 과열)는 None 반환 → 매도 스킵.
        없으면 2분 사이클마다 절반씩 연쇄 매도돼 사실상 전량 익절과 동일해짐.
        """
        import time
        if not hasattr(self, "_rsi_trim_at"):  # __new__ 테스트 하네스 방어
            self._rsi_trim_at = {}
        #: 영속 플래그(rsi_trimmed) 우선 체크 — in-memory 쿨다운은 재시작에
        # 리셋돼 같은 에피소드에 2차 트림 발동 (BAC 02:39 실측: 00:59 재시작 후 재트림).
        # positions.json의 rsi_trimmed가 진짜 에피소드 마커 (추가 매수 시 리셋).
        try:
            if self.positions._get_position(symbol).get("rsi_trimmed"):
                return None
        except Exception:
            pass
        if time.time() - self._rsi_trim_at.get(symbol, 0) < 24 * 3600:
            return None
        self._rsi_trim_at[symbol] = time.time()
        return float(self.config.get("position", {}).get("rsi_trim_ratio", 0.5))

    def _drawdown_multiplier(self) -> float:
        """현재 drawdown 심각도에 따른 포지션 사이즈 multiplier.

        -10%: 0.85× / -15%: 0.70× / -20%: 0.50× / 그 외: 1.00

        effective drawdown 사용 (미결제 타이밍 가짜 dd로 포지션이
        부당하게 축소되던 문제 해결).
        """
        dd = self.tracker.get_effective_drawdown()
        if dd <= -20:
            return 0.50
        if dd <= -15:
            return 0.70
        if dd <= -10:
            return 0.85
        return 1.00

    def _pattern_confidence_boost(self) -> float:
        """최근 30일 패턴 통계로 자가 학습 배수 산출.

        평균 수익률(%) 조건 추가. 작은 익절(+1~2%) 다수가 승률은 100%여도
        평균 수익률이 낮으면 boost하지 않도록. "빠른 익절 강화" 자기학습 함정 방지.

        - 승률 ≥ 70% + 평균 ≥ +500원 + **평균 수익률 ≥ +3%** → 1.25×(적극 상향)
        - 승률 ≥ 55% + 평균 > 0 + **평균 수익률 ≥ +1%**       → 1.05× (안정)
        - 평균 PnL < 0 (누적 손실)                              → 0.85× (보수)
        - 그 외                                                  → 1.00× (중립)

        적극 캡 1.15→1.25: 30일 실증(rsi_overbought 21건 100% +1.3M,
        US 모멘텀 라이딩 HPE +955k/BB +171k)이 있을 때만 발동하는 조건부 증폭.
        10분 캐시로 비용 최소화. [0.85, 1.25] 범위.
        """
        import time as _time
        now = _time.time()
        ts, cached = getattr(self, "_pat_mult_cache", (0.0, 1.0))
        if now - ts < 600 and ts > 0:
            return cached

        try:
            stats = self.tracker.get_pattern_stats(days=30)
        except Exception:
            self._pat_mult_cache = (now, 1.0)
            return 1.0
        total_cnt = sum(s["count"] for s in stats.values())
        total_wins = sum(s["wins"] for s in stats.values())
        total_pnl = sum(s["pnl_sum"] for s in stats.values())
        total_amount = sum(s.get("amount_sum", 0) for s in stats.values())
        if total_cnt == 0:
            self._pat_mult_cache = (now, 1.0)
            return 1.0
        win_rate = total_wins / total_cnt
        avg_pnl = total_pnl / total_cnt
        avg_pct = (total_pnl / total_amount * 100) if total_amount else 0.0

        if win_rate >= 0.70 and avg_pnl >= 500 and avg_pct >= 3.0:
            mult = 1.25
        elif win_rate >= 0.55 and avg_pnl > 0 and avg_pct >= 1.0:
            mult = 1.05
        elif avg_pnl < 0:
            mult = 0.85
        else:
            mult = 1.00
        logger.debug("_pattern_confidence_boost: win=%.0f%% avg_pnl=%+d원 avg_pct=%+.2f%% → ×%.2f",
                     win_rate * 100, int(avg_pnl), avg_pct, mult)
        self._pat_mult_cache = (now, mult)
        return mult

    def _consensus_invest_boost(self, analysis: dict | None) -> tuple[float, str]:
        """Claude 4인 합의 강도를 실제 배팅 크기로 연결."""
        if not analysis:
            return 1.0, "분석 없음"
        details = analysis.get("analyst_details") or {}
        if not details:
            return 1.0, "로컬 전략"

        signals = [d.get("signal", "hold") for d in details.values()]
        buy_count = sum(1 for s in signals if s in ("buy", "long_term_buy"))
        sell_count = sum(1 for s in signals if s == "sell")
        n = len(signals)

        if n >= 2 and (buy_count == n or sell_count == n):
            return self.consensus_unanimous_multiplier, f"만장일치 {max(buy_count, sell_count)}/{n}"
        if n >= 3 and max(buy_count, sell_count) >= n - 1 and min(buy_count, sell_count) == 0:
            return self.consensus_majority_multiplier, f"우세 합의 {max(buy_count, sell_count)}/{n}"
        if buy_count >= 2 and sell_count >= 2:
            return self.consensus_split_multiplier, f"분열 {buy_count}:{sell_count}"
        if buy_count and sell_count:
            return self.consensus_mixed_multiplier, f"약한 충돌 {buy_count}:{sell_count}"
        return 1.0, "중립"

    def _index_allocation_ratios(self) -> dict:
        """현재 KR/US 인덱스 ETF 노출 비중.

        KR ratio = (인덱스 ETF 평가액) / (KR cash + KR 평가액)
        US ratio = (인덱스 ETF USD 평가액) / (USD cash + USD 평가액)
        """
        try:
            kr_bal = self.client.get_balance()
        except Exception:
            kr_bal = {"holdings": [], "cash": 0, "total_eval": 0}
        try:
            us_bal = self.client.get_us_balance()
        except Exception:
            us_bal = {"holdings": [], "cash_usd": 0, "us_eval_usd": 0}

        # KR 할당: KOSPI 추종만 카운트 (360750은 미국 추종이라 KR 베타가 아님)
        kr_index_eval = sum(int(h.get("eval_amount", 0) or 0)
                            for h in kr_bal.get("holdings", [])
                            if h.get("code") in self._INDEX_ETF_KR_KOSPI)
        # US 할당: 본토 SPY/QQQ + KR 거래소의 미국 추종 ETF(360750 등) 모두 US 베타로 카운트
        us_index_eval_usd = sum(float(h.get("eval_amount", 0) or 0)
                                 for h in us_bal.get("holdings", [])
                                 if h.get("ticker") in self._INDEX_ETF_US)
        # 360750 같은 KR 거래 + 미국 추종 ETF의 USD 환산 — fx로 변환해 US 비중에 가산
        try:
            fx = (us_bal.get("total_eval_krw", 0) / max(us_bal.get("total_eval_usd", 1), 1)) \
                 if us_bal.get("total_eval_usd") else self.client.get_usd_krw_rate()
        except Exception:
            fx = 1450.0
        kr_us_hedge_eval_krw = sum(int(h.get("eval_amount", 0) or 0)
                                    for h in kr_bal.get("holdings", [])
                                    if h.get("code") in self._INDEX_ETF_KR_US_HEDGE)
        if fx > 0 and kr_us_hedge_eval_krw > 0:
            us_index_eval_usd += kr_us_hedge_eval_krw / fx

        kr_total = max(int(kr_bal.get("cash", 0) or 0) + int(kr_bal.get("total_eval", 0) or 0), 1)
        # US 분모는 USD 베이스 + KR 거래 미국추종 ETF의 USD 환산
        us_total_usd = max(float(us_bal.get("cash_usd", 0) or 0)
                           + float(us_bal.get("us_eval_usd", 0) or 0)
                           + (kr_us_hedge_eval_krw / fx if fx > 0 else 0), 0.01)

        return {
            "kr": kr_index_eval / kr_total,
            "us": us_index_eval_usd / us_total_usd,
            "kr_index_eval": kr_index_eval,
            "us_index_eval_usd": us_index_eval_usd,
            "kr_total": kr_total,
            "us_total_usd": us_total_usd,
        }

    def _whitelist_min_invest(self, symbol: str, base_asset: float, cash: float,
                              price: float = 0.0, profit_rate: float = 0.0) -> float:
        """핵심(whitelist) 종목 최소 투자금.

        reward/변동성 디레이팅이 비싼 우량주(삼성 325k/주 등)를 qty 0으로 굶겨 'whitelist
        인데도 안 사지던' 근본 원인 해결. 과거 잘못된 패닉 매도로 reward EMA가 망가져도
        핵심주는 conviction 하한만큼 매수하도록 invest를 바닥에서 받쳐준다.

        price가 주어지면 하한을 '최소 1주값' 이상으로 보장 — 하이닉스(2.3M/주)처럼
        conviction 하한(12.5%)이 1주값보다 작아 0주가 되던 케이스 해결.

        비방어 + 비하락장(bear<0.5)에서만, 가용현금 한도 내. KR=KRW, US=USD 공용.
        """
        if not symbol or not self._is_whitelist(symbol) or self._is_inverse(symbol):
            return 0.0
        if getattr(self, "_defensive_mode", False):
            return 0.0
        try:
            if self._bearish_regime_score() >= 0.5:
                return 0.0
        except Exception:
            return 0.0
        pos_cfg = self.config.get("position", {}) or {}
        frac = float(pos_cfg.get("whitelist_conviction_floor", 0.0))
        if frac <= 0:
            return 0.0
        wl_cap = float(self._adaptive_params().get("whitelist_cap", 0.10))
        target_w = wl_cap * frac
        # 물타기(averaging down): 손실 구간이면 목표 비중을 cap 방향으로 키워 더 담아 평단 낮춤.
        # -trigger부터 시작, -max_drawdown에서 cap까지 선형. profit_rate는 분율(예: -0.08).
        if profit_rate < 0:
            trig = float(pos_cfg.get("averaging_down_trigger", -0.05))
            maxdd = float(pos_cfg.get("averaging_down_max_drawdown", -0.12))
            if pos_cfg.get("averaging_down_enabled", True) and profit_rate <= trig and trig > maxdd:
                boost = max(0.0, min(1.0, (trig - profit_rate) / (trig - maxdd)))
                target_w = target_w + (wl_cap - target_w) * boost
        floor = float(base_asset) * target_w
        if price and price > 0:
            floor = max(floor, float(price))  # 비싼 핵심주도 최소 1주 보장
            # 종목별 core_shares 지정이 있으면 그 주수만큼 목표 (하이닉스 2주 등)
            cs = self._whitelist_core_shares(symbol)
            if cs > 0:
                floor = max(floor, cs * float(price))
        return min(float(cash), floor)

    def _vol_target_scalar(self, realized_vol: float) -> float:
        """변동성 타겟 사이징 스칼라(risk-parity-lite).

        종목 실현 일일변동성이 목표보다 크면 작게, 작으면 크게 담아 포트폴리오
        전체 변동성을 일정하게 유지 → 계좌 곡선 안정화. config:vol_sizing 로 조절.
        """
        cfg = self.config.get("vol_sizing", {}) or {}
        if not cfg.get("enabled", False) or not realized_vol or realized_vol <= 0:
            return 1.0
        target = float(cfg.get("target_daily_vol", 0.025))
        lo = float(cfg.get("scalar_min", 0.5))
        hi = float(cfg.get("scalar_max", 1.4))
        return max(lo, min(hi, target / realized_vol))

    def _market_vol_regime(self) -> float:
        """시장 변동성 레짐 기반 총노출 스로틀 (1.0=풀 노출, <1.0=축소→현금버퍼).

        지수 프록시 ETF(KODEX200/QQQ/SPY)의 최근 일별 변동성이 높을수록 신규
        포지션을 축소해 고변동 구간에 dry powder를 남긴다. 평시 1.0이라 external_reserve=0
        '전액 공격'과 충돌하지 않고, 고변동 구간만 일시적으로 노출을 낮춘다. 10분 캐시·Claude無.
        """
        cfg = self.config.get("vol_regime_buffer", {}) or {}
        if not cfg.get("enabled", False):
            return 1.0
        import time as _time
        now = _time.time()
        ts, cached = getattr(self, "_volregime_cache", (0.0, 1.0))
        if now - ts < 600 and ts > 0:
            return cached
        vols: list[float] = []
        for code, _n in self._INDEX_PROXIES_KR:
            try:
                df = self.client.get_ohlcv(code)
                if df is not None and len(df) >= 20:
                    vols.append(float(df["close"].pct_change().dropna().iloc[-20:].std()))
            except Exception:
                continue
        for tk, ex in self._INDEX_PROXIES_US:
            try:
                df = self.client.get_us_ohlcv(tk, exchange=ex)
                if df is not None and len(df) >= 20:
                    vols.append(float(df["close"].pct_change().dropna().iloc[-20:].std()))
            except Exception:
                continue
        if not vols:
            self._volregime_cache = (now, 1.0)
            return 1.0
        avg_vol = sum(vols) / len(vols)
        normal = float(cfg.get("normal_daily_vol", 0.012))
        high = float(cfg.get("high_daily_vol", 0.030))
        floor = float(cfg.get("throttle_floor", 0.6))
        if avg_vol <= normal:
            throttle = 1.0
        elif avg_vol >= high:
            throttle = floor
        else:
            frac = (avg_vol - normal) / (high - normal)
            throttle = 1.0 - frac * (1.0 - floor)
        throttle = max(floor, min(1.0, throttle))
        self._volregime_cache = (now, throttle)
        logger.info("시장변동성 레짐: avg_idx_vol=%.3f (normal %.3f/high %.3f) → 노출 스로틀 ×%.2f",
                    avg_vol, normal, high, throttle)
        return throttle

    def _dynamic_invest_ratio(self, base_ratio: float, confidence: float,
                              is_inverse: bool = False,
                              symbol: str = "",
                              realized_vol: float = 0.0) -> tuple[float, str]:
        """확신도 + 하락 국면 점수에 따라 base invest_ratio를 조정.

        일반주 (is_inverse=False):
          - 확신도 0.5~1.0을 0.7~1.3 multiplier에 선형 매핑
          - bear 점수가 높으면 일반주 노출은 축소 (× (1 - bear × 0.4))
          - defensive 모드면 추가 0.7배
          - 범위 [0.3, 1.5]로 클램프

        인버스 (is_inverse=True) — 분할 매수:
          - bear 0.50 미만 = 0 (진입 gate에서 이미 차단될 것)
          - bear 0.50~0.65 = 0.35배 (1차 진입, 소규모)
          - bear 0.65~0.80 = 0.60배 (2차 증액)
          - bear 0.80+      = 0.85배 (3차 풀)
          - tension/crisis/war면 각 구간 +0.15배
        """
        bear = self._bearish_regime_score()

        if is_inverse:
            if bear >= 0.80:
                mult = 0.85
            elif bear >= 0.65:
                mult = 0.60
            elif bear >= 0.50:
                mult = 0.35
            else:
                mult = 0.35  # market_condition tension 이상으로 진입한 경우
            if getattr(self, "_market_condition", "peace") in ("tension", "crisis", "war"):
                mult = min(1.0, mult + 0.15)
            reason = f"인버스 bear={bear:.2f} → ×{mult:.2f}"
            return base_ratio * mult, reason

        conf = max(0.0, min(1.0, confidence or 0.5))
        conf_mult = 0.7 + (conf - 0.5) * 1.2  # 0.5→0.7, 1.0→1.3
        regime_mult = max(0.4, 1.0 - bear * 0.4)  # bear 1.0 → 0.6
        pat_mult = self._pattern_confidence_boost()  # 최근 30일 패턴 승률 기반 [0.85, 1.25]
        dd_mult = self._drawdown_multiplier()  # drawdown -10/-15/-20 구간에서 0.85/0.70/0.50
        # Kelly criterion (MC 통계 기반, Half-Kelly + 보수적 클램프)
        kelly_mult = self._kelly_fraction(getattr(self, "_last_mc_stats", None))
        # 레버:
        # vol_scalar — 종목 변동성 타겟 사이징 (변동성↑→작게, risk-parity-lite)
        # regime_throttle— 시장 고변동 구간 총노출 축소 (현금버퍼)
        vol_scalar = self._vol_target_scalar(realized_vol)
        regime_throttle = self._market_vol_regime()
        # AI 신호(크로스시그널·데일리 편향) 사이징 반영 — 종목 단위, 중립이면 1.0.
        # 강세 편향이면 키우고 약세 편향이면 줄인다 ([0.7,1.3] 범위는 _ai_signal_for가 보장).
        ai_mult = 1.0
        if symbol:
            try:
                ai_mult = float(self._ai_signal_for(
                    self._market_for_code(symbol), symbol).get("size_mult", 1.0))
            except Exception:
                ai_mult = 1.0
        mult = (conf_mult * regime_mult * pat_mult * dd_mult * kelly_mult
                * vol_scalar * regime_throttle * ai_mult)
        defensive = bool(getattr(self, "_defensive_mode", False))
        if defensive:
            mult *= 0.7
        mult = max(0.2, min(1.5, mult))  # 드로우다운 최대 감쇠까지 허용
        final_ratio = base_ratio * mult
        # adaptive cap — whitelist 종목은 별도 큰 cap 적용
        adapt = self._adaptive_params()
        if symbol and self._is_whitelist(symbol):
            max_ratio_cap = adapt.get("whitelist_cap", adapt.get("cap", 0.10))
        else:
            max_ratio_cap = adapt.get("cap", self.config.get("invest_ratio_max", 0.0))
        if max_ratio_cap > 0:
            final_ratio = min(final_ratio, max_ratio_cap)

        #: 핵심(whitelist) 종목 확신 하한.
        # base×mult가 곱셈식 디레이팅(conf·regime·pattern·dd·kelly)으로 짓눌려
        # 삼성/하이닉스 같은 핵심주가 cap(25%)에 한참 못 미치는 3~9%에 머물고,
        # 1차 tranche가 1~2주로 쪼그라들던 문제 보완. 강세장+비방어에서만 적용 —
        # 약세장/방어 모드는 기존 보수화 유지(하한 미적용). 하드 ceiling은 그대로 cap.
        floor_note = ""
        if (symbol and self._is_whitelist(symbol) and not defensive and max_ratio_cap > 0):
            pos_cfg = self.config.get("position", {}) or {}
            floor_frac = float(pos_cfg.get("whitelist_conviction_floor", 0.0))
            bull_min = float(pos_cfg.get("whitelist_floor_bull_min", 0.5))
            if floor_frac > 0:
                try:
                    bull = self._bullish_regime_score()
                except Exception:
                    bull = 0.0
                # 확신도 비례 — 낮은 conf면 하한도 비례 축소 (0.7+에서 풀)
                conf_scale = max(0.0, min(1.0, conf / 0.7))
                if bull >= bull_min and conf_scale > 0:
                    floor = max_ratio_cap * floor_frac * conf_scale
                    if final_ratio < floor:
                        final_ratio = floor
                        floor_note = f" | 핵심주 하한 {floor:.0%} (bull {bull:.2f}, conf {conf:.2f})"

        reason = (f"일반 conf={conf:.2f} bear={bear:.2f} pat={pat_mult:.2f} "
                  f"dd={dd_mult:.2f} kelly={kelly_mult:.2f} vol={vol_scalar:.2f} "
                  f"regime={regime_throttle:.2f} ai={ai_mult:.2f} defensive={defensive} → ×{mult:.2f}"
                  + (f" (cap {max_ratio_cap:.0%})" if max_ratio_cap > 0 and final_ratio == max_ratio_cap else "")
                  + floor_note)
        return final_ratio, reason
