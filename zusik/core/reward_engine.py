from __future__ import annotations
"""보상 엔진 — 수익 기반 학습 + 가중치 시스템.

핵심 원리:
  - 높은 실현수익을 낸 전략/종목/시장조건에 가중치(보상)를 부여
  - 가중치가 높을수록 더 많은 자본을 배분
  - 승리 패턴(어떤 지표 조합에서 수익이 났는지)을 기록하고 재활용
  - 연속 수익 시 보너스 가중치(모멘텀 보상)
  - 시간이 지나면 가중치가 서서히 감소(decay) → 최근 성과에 집중

저장: data/reward_state.json
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

REWARD_FILE = os.path.join("data", "reward_state.json")

# 2026-08 실거래에서 청산 사유를 진입 컨텍스트로 저장해
# peace:long:rsi_overbought 37건/36승이 "과매수 매수 승률 97.3%"로 프롬프트에
# 유출됐다. 이 라벨들은 진입 품질이 아니라 출구 품질이므로 자본 배분/매수 프롬프트에서 제외한다.
EXIT_ONLY_CONTEXT_SUFFIXES = {
    "rsi_overbought", "split_profit", "breakeven_protect", "trailing_stop",
    "forced_stop", "slow_bleed", "crash_instant", "quick_loss", "rotate",
    "ambiguous_take", "inverse_take", "inverse_eod_lock",
}


def _load() -> dict:
    if os.path.exists(REWARD_FILE):
        with open(REWARD_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    # 원자적 쓰기: 파손 JSON 클래스 차단
    tmp = REWARD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REWARD_FILE)


class RewardEngine:
    """수익 기반 보상/가중치 시스템."""

    def __init__(self, config: dict):
        reward_cfg = config.get("reward", {})

        # 기본 설정
        self.decay_rate: float = reward_cfg.get("decay_rate", 0.95)  # 일일 가중치 감쇠율
        self.streak_bonus: float = reward_cfg.get("streak_bonus", 0.1)  # 연속 수익 보너스 (+10%/회)
        self.max_boost: float = reward_cfg.get("max_boost", 3.0)  # 최대 가중치 배수
        self.min_weight: float = reward_cfg.get("min_weight", 0.3)  # 최소 가중치
        self.learning_trades: int = reward_cfg.get("learning_trades", 5)  # 학습에 필요한 최소 거래 수
        self.context_learning_trades: int = reward_cfg.get("context_learning_trades", 3)
        self.context_return_scale: float = reward_cfg.get("context_return_scale", 8.0)
        self.context_win_bonus_scale: float = reward_cfg.get("context_win_bonus_scale", 1.5)

        # 상태 로드
        state = _load()
        self._strategy_scores: dict = state.get("strategy_scores", {})
        self._stock_scores: dict = state.get("stock_scores", {})
        self._context_scores: dict = state.get("context_scores", {})
        self._win_patterns: list = state.get("win_patterns", [])
        self._loss_patterns: list = state.get("loss_patterns", [])
        self._streak: int = state.get("streak", 0)  # 연속 수익 횟수 (음수면 연속 손실)
        self._total_trades: int = state.get("total_trades", 0)
        self._last_decay_date: str = state.get("last_decay_date", "")

        # 시작 시 decay 적용
        self._apply_daily_decay()

    # ══════════════════════════════════════
    # 1. 거래 결과 기록 + 보상 계산
    # ══════════════════════════════════════

    def record_trade_result(
        self,
        stock_code: str,
        stock_name: str,
        strategy_name: str,
        realized_pnl: int,
        realized_rate: float,
        indicators: dict | None = None,
        signal: str = "",
        context: str = "",
    ):
        """매도 체결 후 호출 — 실현손익 기반으로 보상 갱신.

        Args:
            realized_pnl: 실현손익 (원)
            realized_rate: 실현수익률 (%)
            indicators: 매수 시점의 기술적 지표 (패턴 학습용)
            signal: 매수 시 사용된 신호 ("buy", "long_term_buy")
        """
        self._total_trades += 1
        is_win = realized_pnl > 0

        # ── 전략 점수 갱신 ──
        s = self._get_strategy_score(strategy_name)
        s["trades"] += 1
        s["total_pnl"] += realized_pnl
        if is_win:
            s["wins"] += 1
            s["total_win_pnl"] += realized_pnl
        else:
            s["losses"] += 1
            s["total_loss_pnl"] += realized_pnl

        # 가중치 계산: 최근 수익률 기반 EMA
        alpha = 0.3  # EMA 가중치 (최근 거래에 더 비중)
        s["ema_return"] = alpha * realized_rate + (1 - alpha) * s.get("ema_return", 0)
        s["last_updated"] = datetime.now().isoformat()

        # ── 종목 점수 갱신 ──
        st = self._get_stock_score(stock_code, stock_name)
        st["trades"] += 1
        st["total_pnl"] += realized_pnl
        if is_win:
            st["wins"] += 1
        st["ema_return"] = alpha * realized_rate + (1 - alpha) * st.get("ema_return", 0)
        st["last_updated"] = datetime.now().isoformat()

        # ── 상황별 점수 갱신 ──
        if context:
            ctx = self._get_context_score(context)
            ctx["trades"] += 1
            ctx["total_pnl"] += realized_pnl
            if is_win:
                ctx["wins"] += 1
            ctx["ema_return"] = alpha * realized_rate + (1 - alpha) * ctx.get("ema_return", 0)
            ctx["last_updated"] = datetime.now().isoformat()

        # ── 연속 수익/손실 추적 ──
        if is_win:
            self._streak = max(self._streak, 0) + 1
        else:
            self._streak = min(self._streak, 0) - 1

        # ── 패턴 기록 (지표 조합 학습) ──
        if indicators:
            pattern = self._extract_pattern(indicators, realized_rate, signal)
            if is_win:
                self._win_patterns.append(pattern)
                # 최근 100개만 유지
                self._win_patterns = self._win_patterns[-100:]
            else:
                self._loss_patterns.append(pattern)
                self._loss_patterns = self._loss_patterns[-50:]

        self._save_state()

        reward = self.get_strategy_weight(strategy_name)
        logger.info(
            "보상 엔진: %s %s | 수익률 %+.2f%% | 전략 '%s' 가중치 %.2f | 연속 %s%d회",
            stock_name, "WIN" if is_win else "LOSS",
            realized_rate, strategy_name, reward,
            "수익 " if self._streak > 0 else "손실 ", abs(self._streak),
        )

    # ══════════════════════════════════════
    # 2. 가중치/배분 계산
    # ══════════════════════════════════════

    def get_strategy_weight(self, strategy_name: str) -> float:
        """전략의 현재 가중치 (자본 배분 배수).

        Returns:
            0.3 ~ 3.0 사이의 배수.
            1.0 = 기본, >1.0 = 더 많은 자본 배분, <1.0 = 축소
        """
        s = self._get_strategy_score(strategy_name)

        if s["trades"] < self.learning_trades:
            return 1.0  # 데이터 부족 시 기본값

        # 기본 점수: EMA 수익률 기반
        base = 1.0 + s["ema_return"] * 10  # 수익률 1% → 가중치 +0.1

        # 승률 보너스
        win_rate = s["wins"] / s["trades"] if s["trades"] > 0 else 0.5
        win_bonus = (win_rate - 0.5) * 2  # 승률 50% → 0, 70% → +0.4

        # 연속 수익 보너스
        streak_bonus = 0
        if self._streak > 0:
            streak_bonus = self._streak * self.streak_bonus

        weight = base + win_bonus + streak_bonus

        # 클램프
        return max(self.min_weight, min(self.max_boost, weight))

    def get_stock_weight(self, stock_code: str) -> float:
        """종목의 현재 가중치."""
        st = self._get_stock_score(stock_code)

        if st["trades"] < 3:
            return 1.0

        base = 1.0 + st["ema_return"] * 10
        win_rate = st["wins"] / st["trades"] if st["trades"] > 0 else 0.5
        win_bonus = (win_rate - 0.5) * 2

        return max(self.min_weight, min(self.max_boost, base + win_bonus))

    def get_context_weight(self, context: str = "") -> float:
        """시장/셋업 컨텍스트 가중치."""
        if not context:
            return 1.0

        # 구 reward_state에 남은 청산 라벨은 진입 배수로 재사용하지 않는다.
        if context.rsplit(":", 1)[-1] in EXIT_ONLY_CONTEXT_SUFFIXES:
            return 1.0

        ctx = self._get_context_score(context)
        if ctx["trades"] < self.context_learning_trades:
            return 1.0

        base = 1.0 + ctx["ema_return"] * self.context_return_scale
        win_rate = ctx["wins"] / ctx["trades"] if ctx["trades"] > 0 else 0.5
        win_bonus = (win_rate - 0.5) * self.context_win_bonus_scale
        return max(self.min_weight, min(self.max_boost, base + win_bonus))

    def get_invest_multiplier(self, strategy_name: str, stock_code: str, context: str = "") -> float:
        """최종 투자금 배수 = 전략 가중치 × 종목 가중치 × 연속보너스.

        봇이 기본 투자금에 이 배수를 곱하여 실제 투자금을 결정.
        """
        sw = self.get_strategy_weight(strategy_name)
        stw = self.get_stock_weight(stock_code)
        cw = self.get_context_weight(context)

        # 기하평균으로 합산 (한쪽만 높아도 과도하게 올라가지 않음)
        combined = (sw * stw * cw) ** (1 / 3)

        return max(self.min_weight, min(self.max_boost, combined))

    # ══════════════════════════════════════
    # 3. 승리 패턴 학습
    # ══════════════════════════════════════

    @staticmethod
    def _extract_pattern(indicators: dict, realized_rate: float, signal: str) -> dict:
        """지표 조합에서 핵심 패턴 추출."""
        return {
            "rsi": indicators.get("RSI_14"),
            "macd_hist": indicators.get("MACD_히스토그램"),
            "ma_aligned": indicators.get("정배열"),
            "vol_ratio": indicators.get("거래량_비율"),
            "bb_position": _bb_position(indicators),
            "volatility": indicators.get("20일_변동성"),
            "signal": signal,
            "source": "entry",
            "realized_rate": realized_rate,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }

    def get_winning_conditions(self) -> dict:
        """과거 수익 거래에서 공통 조건 추출 — Claude에게 전달용.

        Returns:
            {
                "avg_rsi_at_buy": 평균 RSI,
                "preferred_ma_aligned": True/False,
                "avg_vol_ratio": 평균 거래량 비율,
                "best_bb_position": "하단/중간/상단",
                "win_count": 수익 거래 수,
                "avg_win_rate": 평균 수익률,
                "summary": 사람이 읽을 수 있는 요약,
            }
        """
        # 구 상태는 매도 시점 지표를 "매수 당시"로 잘못 기록했다(source 없음).
        # 검증 가능한 신규 진입 표본만 사용해 과매수 청산 특성이 매수 선호로 새지 않게 한다.
        win_patterns = [p for p in self._win_patterns if p.get("source") == "entry"]
        loss_patterns = [p for p in self._loss_patterns if p.get("source") == "entry"]
        if len(win_patterns) < self.learning_trades:
            return {"summary": "아직 학습 데이터 부족 (최소 {}건 필요)".format(self.learning_trades)}

        rsis = [p["rsi"] for p in win_patterns if p.get("rsi") is not None]
        vol_ratios = [p["vol_ratio"] for p in win_patterns if p.get("vol_ratio") is not None]
        ma_aligned = [p["ma_aligned"] for p in win_patterns if p.get("ma_aligned") is not None]
        rates = [p["realized_rate"] for p in win_patterns]
        bb_positions = [p["bb_position"] for p in win_patterns if p.get("bb_position")]

        avg_rsi = sum(rsis) / len(rsis) if rsis else None
        avg_vol = sum(vol_ratios) / len(vol_ratios) if vol_ratios else None
        pref_ma = sum(ma_aligned) / len(ma_aligned) > 0.5 if ma_aligned else None
        avg_rate = sum(rates) / len(rates) if rates else 0
        best_bb = max(set(bb_positions), key=bb_positions.count) if bb_positions else None

        parts = []
        if avg_rsi is not None:
            parts.append(f"RSI {avg_rsi:.0f} 부근에서 매수 시 수익 확률 높음")
        if pref_ma is True:
            parts.append("이동평균 정배열일 때 승률 높음")
        elif pref_ma is False:
            parts.append("정배열 아닌 구간에서도 수익 실현 가능")
        if avg_vol is not None and avg_vol > 1.5:
            parts.append(f"거래량이 평균 {avg_vol:.1f}배 이상일 때 유리")
        if best_bb:
            parts.append(f"볼린저밴드 {best_bb} 부근에서 진입 시 유리")

        result = {
            "avg_rsi_at_buy": round(avg_rsi, 1) if avg_rsi else None,
            "preferred_ma_aligned": pref_ma,
            "avg_vol_ratio": round(avg_vol, 2) if avg_vol else None,
            "best_bb_position": best_bb,
            "win_count": len(win_patterns),
            "loss_count": len(loss_patterns),
            "avg_win_rate": round(avg_rate, 2),
            "streak": self._streak,
            "summary": " / ".join(parts) if parts else "뚜렷한 패턴 미발견",
        }
        return result

    def get_losing_conditions(self) -> dict:
        """과거 손실 거래의 공통 조건 — 회피용."""
        loss_patterns = [p for p in self._loss_patterns if p.get("source") == "entry"]
        if len(loss_patterns) < 3:
            return {"summary": "손실 데이터 부족"}

        rsis = [p["rsi"] for p in loss_patterns if p.get("rsi") is not None]
        rates = [p["realized_rate"] for p in loss_patterns]

        avg_rsi = sum(rsis) / len(rsis) if rsis else None
        avg_rate = sum(rates) / len(rates) if rates else 0

        parts = []
        if avg_rsi is not None:
            parts.append(f"RSI {avg_rsi:.0f} 부근 진입 시 손실 빈번")
        parts.append(f"평균 손실률 {avg_rate:.2f}%")

        return {
            "avg_rsi_at_loss": round(avg_rsi, 1) if avg_rsi else None,
            "avg_loss_rate": round(avg_rate, 2),
            "loss_count": len(loss_patterns),
            "summary": " / ".join(parts),
        }

    # ══════════════════════════════════════
    # 4. 전체 성과 리포트
    # ══════════════════════════════════════

    def get_performance_report(self) -> dict:
        """전략/종목별 성과 + 가중치 전체 리포트."""
        strategies = {}
        for name, s in self._strategy_scores.items():
            strategies[name] = {
                **s,
                "win_rate": round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0,
                "weight": round(self.get_strategy_weight(name), 2),
            }

        stocks = {}
        for code, st in self._stock_scores.items():
            stocks[code] = {
                **st,
                "win_rate": round(st["wins"] / st["trades"] * 100, 1) if st["trades"] > 0 else 0,
                "weight": round(self.get_stock_weight(code), 2),
            }

        contexts = {}
        for name, ctx in self._context_scores.items():
            contexts[name] = {
                **ctx,
                "win_rate": round(ctx["wins"] / ctx["trades"] * 100, 1) if ctx["trades"] > 0 else 0,
                "weight": round(self.get_context_weight(name), 2),
            }

        return {
            "total_trades": self._total_trades,
            "streak": self._streak,
            "strategies": strategies,
            "stocks": stocks,
            "contexts": contexts,
            "winning_conditions": self.get_winning_conditions(),
            "losing_conditions": self.get_losing_conditions(),
        }

    def get_performance_summary_text(self) -> str:
        """Claude에게 전달할 성과 요약 텍스트."""
        report = self.get_performance_report()
        lines = [f"총 {report['total_trades']}건 거래"]

        if report["streak"] > 0:
            lines.append(f"현재 {report['streak']}연속 수익 중")
        elif report["streak"] < 0:
            lines.append(f"현재 {abs(report['streak'])}연속 손실 중")

        for name, s in report["strategies"].items():
            if s["trades"] > 0:
                lines.append(
                    f"전략 '{name}': {s['trades']}건, 승률 {s['win_rate']}%, "
                    f"누적 {s['total_pnl']:+,}원, 가중치 {s['weight']}"
                )

        top_contexts = sorted(
            ((name, c) for name, c in report["contexts"].items()
             if c["trades"] > 0
             and name.rsplit(":", 1)[-1] not in EXIT_ONLY_CONTEXT_SUFFIXES),
            key=lambda item: item[1]["weight"],
            reverse=True,
        )[:3]
        for name, c in top_contexts:
            lines.append(
                f"상황 '{name}': {c['trades']}건, 승률 {c['win_rate']}%, "
                f"누적 {c['total_pnl']:+,}원, 가중치 {c['weight']}"
            )

        wc = report["winning_conditions"]
        if wc.get("summary"):
            lines.append(f"승리 패턴: {wc['summary']}")

        lc = report["losing_conditions"]
        if lc.get("summary"):
            lines.append(f"손실 패턴: {lc['summary']}")

        return " | ".join(lines)

    # ══════════════════════════════════════
    # 내부
    # ══════════════════════════════════════

    def _get_strategy_score(self, name: str) -> dict:
        if name not in self._strategy_scores:
            self._strategy_scores[name] = {
                "trades": 0, "wins": 0, "losses": 0,
                "total_pnl": 0, "total_win_pnl": 0, "total_loss_pnl": 0,
                "ema_return": 0, "last_updated": "",
            }
        return self._strategy_scores[name]

    def _get_stock_score(self, code: str, name: str = "") -> dict:
        if code not in self._stock_scores:
            self._stock_scores[code] = {
                "name": name, "trades": 0, "wins": 0,
                "total_pnl": 0, "ema_return": 0, "last_updated": "",
            }
        return self._stock_scores[code]

    def _get_context_score(self, context: str) -> dict:
        if context not in self._context_scores:
            self._context_scores[context] = {
                "trades": 0, "wins": 0,
                "total_pnl": 0, "ema_return": 0, "last_updated": "",
            }
        return self._context_scores[context]

    def _apply_daily_decay(self):
        """하루에 한 번, 모든 가중치에 감쇠 적용 — 최근 성과에 집중."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_decay_date == today:
            return

        for s in self._strategy_scores.values():
            s["ema_return"] *= self.decay_rate
        for st in self._stock_scores.values():
            st["ema_return"] *= self.decay_rate
        for ctx in self._context_scores.values():
            ctx["ema_return"] *= self.decay_rate

        self._last_decay_date = today
        self._save_state()

    def _save_state(self):
        _save({
            "strategy_scores": self._strategy_scores,
            "stock_scores": self._stock_scores,
            "context_scores": self._context_scores,
            "win_patterns": self._win_patterns,
            "loss_patterns": self._loss_patterns,
            "streak": self._streak,
            "total_trades": self._total_trades,
            "last_decay_date": self._last_decay_date,
        })


def _bb_position(indicators: dict) -> str | None:
    """현재가가 볼린저밴드 어디에 위치하는지."""
    price = indicators.get("현재가")
    upper = indicators.get("볼린저_상단")
    lower = indicators.get("볼린저_하단")
    if not all([price, upper, lower]) or upper == lower:
        return None
    ratio = (price - lower) / (upper - lower)
    if ratio <= 0.3:
        return "하단"
    elif ratio >= 0.7:
        return "상단"
    return "중간"
