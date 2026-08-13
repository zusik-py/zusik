from __future__ import annotations

import logging


from zusik.analysis.smart_signals import SmartSignals


logger = logging.getLogger(__name__)


class InverseHedgeMixin:
    """InverseHedgeMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _inverse_kr_codes(self) -> set[str]:
        return set(SmartSignals.KR_INVERSE_ETF.keys())

    def _is_inverse(self, symbol: str) -> bool:
        if not symbol:
            return False
        return symbol in self._inverse_kr_codes() or symbol in self._inverse_us_tickers()

    @staticmethod
    def _inverse_deep_collapse(crash: dict | None) -> bool:
        """인버스 헷지 손절 게이트(레짐기반).

        인버스는 헷지다. 지수 반등으로 인버스가 자기 차트 -7%(crash_instant)를 찍는 건
        '헷지 역할 종료' 신호지 손절 사유가 아니다 — controlled 강제청산
        (_should_force_exit_inverse: peace+bear<0.25)이 통제된 시점에 정리한다.
        여기서는 자본보호용 깊은 붕괴(-15%↓ / 고점 급락 crash_from_high)만 True.
        반등일 인버스 crash_instant -66k 바닥투매 회귀 방지.
        """
        if not crash:
            return False
        return (crash.get("action") == "crash_from_high"
                or crash.get("change", 0) <= -0.15)

    def _inverse_kr_list(self) -> list[dict]:
        #: 기본 스캔/매수는 -1X 지수매칭(default=True)만. -2X는 decay로 데이터상
        # 열위라 crisis_only로 제외 (보유 시 _is_inverse는 여전히 전체 인식 → 청산은 정상).
        if not getattr(self, "derivative_etf_enabled", True):
            return []
        return [{"code": c, "name": meta["name"]}
                for c, meta in SmartSignals.KR_INVERSE_ETF.items() if meta.get("default")]

    def _bearish_regime_score(self) -> float:
        """하락 국면 점수 (0.0~1.0). Claude 호출 없이 로컬 연산.

        산출 우선순위:
          1. 지수 프록시 ETF (KOSPI=069500, NASDAQ=QQQ, S&P=SPY) 평균 모멘텀 — 주요 신호
          2. 보유 종목 평균 모멘텀 — 포트폴리오 특화 (가중치 낮음)

        점수 공식: tanh 압축된 momentum_score를 -1~+1에서 역치시켜 0~1로 매핑.
        10분 캐시.
        """
        import time as _time
        now = _time.time()
        ts, cached = getattr(self, "_bear_cache", (0.0, 0.0))
        if now - ts < 600 and ts > 0:
            return cached

        from zusik.analysis.indicators import momentum_score

        index_scores: list[float] = []
        intraday_changes: list[float] = []  # 지수 프록시 현재가 등락률 (장중 폭락 반영)
        for code, _name in self._INDEX_PROXIES_KR:
            try:
                df = self.client.get_ohlcv(code)
                if df is not None and len(df) >= 20:
                    index_scores.append(momentum_score(df))
            except Exception:
                pass
            try:
                cr = float(self.client.get_current_price(code).get("change_rate", 0) or 0)
                intraday_changes.append(cr / 100.0)
            except Exception:
                pass
        for ticker, exchange in self._INDEX_PROXIES_US:
            try:
                df = self.client.get_us_ohlcv(ticker, exchange=exchange)
                if df is not None and len(df) >= 20:
                    index_scores.append(momentum_score(df))
            except Exception:
                pass
            try:
                # 미국 장중일 때만 — 마감 후엔 전일 등락률이 얼어붙은 채 min()에 잡혀
                # KR 장중 반등을 가림 (라이브: 정지된 QQQ -1.9%가 bear를 0.32에 고정,
                # 청산 임계 0.25 미달 지속 → KOSPI V반등에도 인버스 홀드 → 갭 손실)
                if self.client.us_market_phase() == "open":
                    cr = float(self.client.get_us_current_price(ticker, exchange).get("change_rate", 0) or 0)
                    intraday_changes.append(cr / 100.0)
            except Exception:
                pass

        holding_scores: list[float] = []
        try:
            kr_bal = self.client.get_balance()
            for h in kr_bal.get("holdings", [])[:5]:
                code = h.get("code")
                if not code or self._is_inverse(code):
                    continue
                try:
                    df = self.client.get_ohlcv(code)
                    if df is not None and len(df) >= 20:
                        holding_scores.append(momentum_score(df))
                except Exception:
                    continue
        except Exception:
            pass
        try:
            us_bal = self.client.get_us_balance()
            for h in us_bal.get("holdings", [])[:5]:
                tk = h.get("ticker")
                if not tk or self._is_inverse(tk):
                    continue
                try:
                    df = self.client.get_us_ohlcv(tk, exchange=h.get("exchange", "NASD"))
                    if df is not None and len(df) >= 20:
                        holding_scores.append(momentum_score(df))
                except Exception:
                    continue
        except Exception:
            pass

        if index_scores and holding_scores:
            avg_idx = sum(index_scores) / len(index_scores)
            avg_hold = sum(holding_scores) / len(holding_scores)
            # 지수 70%, 보유 30% 가중
            avg_m = avg_idx * 0.7 + avg_hold * 0.3
        elif index_scores:
            avg_m = sum(index_scores) / len(index_scores)
        elif holding_scores:
            avg_m = sum(holding_scores) / len(holding_scores)
        else:
            avg_m = 0.0

        score = max(0.0, min(1.0, -avg_m))
        # 장중 급락 보강: 20일 일봉 모멘텀은 당일 폭락을 반영 못 한다
        # (폭락일 bear=0.00 고정 → 인버스 사이징·익절틸트가 평시처럼 작동하던 버그).
        # 지수 프록시 현재가 등락률을 0~1 bear로 매핑(-3%→0.5, -6%→1.0)해 max로 결합.
        intraday_bear = 0.0
        if intraday_changes:
            worst = min(intraday_changes)  # 가장 큰 하락폭 (음수)
            intraday_bear = max(0.0, min(1.0, -worst / 0.06))
            score = max(score, intraday_bear)
        self._bear_cache = (now, score)
        logger.info("bearish_regime_score=%.2f (index_n=%d, hold_n=%d, intraday_bear=%.2f)",
                    score, len(index_scores), len(holding_scores), intraday_bear)
        return score

    def _should_allow_inverse_entry(self) -> tuple[bool, str]:
        """인버스 ETF 신규 매수 허용 여부 — '진짜 급락장'에서만.

        KIS 2년 백테스트 결론: 강세장에선 어떤 인버스도 손실(-23~-97%). pullback 추격 금지.
        → 평시·긴장(pullback)엔 매수 안 함. 아래 둘 중 하나(진짜 급락)에만 발동:
          A) market_condition ∈ {crisis, war} (뉴스/거시 위기, detect_market_condition)
          B) 지수 프록시 sharp 급락 (_index_crash: 1일-3.5%/3일-7%)
        기존 bear≥0.50(2년간 0회 발화) 및 tension 진입은 제거 — 데이터상 손실/무의미.
        """
        if not getattr(self, "derivative_etf_enabled", True):
            return False, "파생ETF 미신청 (broker.derivative_etf_enabled=false)"
        inv = (self.config.get("inverse", {}) or {})
        if not inv.get("enabled", False):
            return False, "인버스 비활성 (config.inverse.enabled=false)"
        # 마감 임박(EOD 락인 윈도) 신규 진입 금지 — 장중 이득은 몇 분뿐인데 익일 개장
        # 갭 위험은 전부 진다(인버스 비대칭 갭). 라이브 07-16: 15:06 EOD 수익 락인 매도
        # 12분 뒤 급락 가드가 같은 종목을 되사서 락인 목적을 무효화 + 왕복 수수료.
        try:
            window = int(inv.get("eod_lock_window_min", 20) or 20)
            for _mtc in (self.client.minutes_to_close(), self.client.us_minutes_to_close()):
                if _mtc is not None and 0 <= _mtc <= window:
                    return False, f"마감 {_mtc}분 전 — EOD 윈도 내 신규 헷지 금지 (익일 갭 위험)"
        except Exception:
            pass
        mc = getattr(self, "_market_condition", "peace")
        if inv.get("trigger_crisis", True) and mc in ("crisis", "war"):
            return True, f"진짜 하락장 ({mc}) — 인버스 헷지 발동"
        # 빠른 시장 급락 가드(메가캡발/지수 급락) — _check_risks_before_trading 에서 세팅. 헷지 발동.
        if getattr(self, "_fast_fall_active", False):
            return True, "급락 가드 발동 — 인버스 헷지"
        # 단발 지수 급락 진입은 V반등 휩쏘 위험 — config 로 명시 활성 시에만 (기본 OFF)
        if inv.get("trigger_index_crash", False) and self._index_crash():
            return True, "지수 급락 감지 (1일-3.5%/3일-7%) — 인버스 헷지 발동"
        return False, f"급락 아님 (market={mc}) — 인버스 대기 (지속 위기 시만)"

    def _should_force_exit_inverse(self) -> tuple[bool, str]:
        """보유 인버스를 선제 청산할지. 급락 종료 + peace 복귀 + bearish 해소 시 True.

        진입이 crisis/war 또는 _index_crash로 바뀌어서, 급락 진행 중엔
        절대 청산하지 않도록 _index_crash 가드 추가 (매수 직후 즉시 청산되는 churn 방지).
        """
        mc = getattr(self, "_market_condition", "peace")
        if mc != "peace":
            return False, ""
        if self._index_crash():            # 급락 진행 중 → 헷지 유지
            return False, ""
        score = self._bearish_regime_score()
        if score < 0.25:
            return True, f"평시 복귀 + 급락 해소 + bear {score:.2f} < 0.25 — 인버스 청산"
        # 반등 확인 청산: KR 지수 프록시가 당일 +0.5% 이상 반등이면 헷지 명제(지수 급락)
        # 자체가 무효 — bear 점수가 0.25~0.50 데드존에 걸려 있어도 나온다. 일봉 모멘텀·
        # 스테일 프록시가 점수를 붙들어 V반등일에 홀드→익일 갭 손실 나던 패턴 차단.
        try:
            kospi_code = self._INDEX_PROXIES_KR[0][0]
            cr = float(self.client.get_current_price(kospi_code).get("change_rate", 0) or 0)
            if cr >= 0.5:
                return True, (f"지수 당일 반등 +{cr:.1f}% (bear {score:.2f} 데드존 무시) — "
                              f"헷지 명제 해소, 인버스 청산")
        except Exception:
            pass
        return False, ""

    def _inverse_eod_lock_due(self, market: str, holding: "dict | None",
                              cur_price: float = 0.0) -> "tuple[bool, str]":
        """마감 임박 + 왕복 수수료 공제 후 순익(+)인 인버스 → 익일 개장 갭 전 수익 락인.

        인버스는 '헷지가 통한 바로 그 하락'이 익일 개장 반등(갭업)에 되돌려져 장중 평가익이
        증발하는 비대칭 갭 위험이 크다(사용자 수동 매도 사유). 정규장 마감 N분 전부터 순익이
        임계 이상이면 실현한다. 레짐이 유효하면 익일 다시 진입(_should_allow_inverse_entry).
        매도 reason에 손절/급락/트레일 키워드가 없어 session 태그(12h)로 등록 → 같은 세션
        재매수(churn)는 _churn_guard가 자동 차단, 다음 세션 재진입은 허용.
        config: inverse.eod_profit_lock / eod_lock_window_min / eod_lock_min_profit.
        """
        cfg = (self.config.get("inverse", {}) or {})
        if not cfg.get("eod_profit_lock", True):
            return False, ""
        if not holding:
            return False, ""
        qty = float(holding.get("qty", 0) or 0)
        if qty <= 0:
            return False, ""
        mtc = (self.client.minutes_to_close() if market == "KR"
               else self.client.us_minutes_to_close())
        if mtc is None or mtc < 0:
            return False, ""
        window = float(cfg.get("eod_lock_window_min", 20))
        if mtc > window:
            return False, ""
        net_rate = self._inverse_net_rate(market, holding, cur_price)
        min_profit = float(cfg.get("eod_lock_min_profit", 0.003))
        if net_rate < min_profit:
            return False, ""
        return True, (f"인버스 EOD 수익 락인 (마감 {mtc:.0f}분 전, 순익 {net_rate * 100:+.2f}%) "
                      f"— 익일 개장 갭 전 실현")

    @staticmethod
    def _intraday_change(price, df):
        """장중 등락률 = (현재가 − 직전봉 종가)/직전봉 종가. df 부족하면 None."""
        try:
            c = df["close"].values
            prev = float(c[-2])
            return (float(price) - prev) / prev if prev > 0 else 0.0
        except Exception:
            return None

    def _inverse_priority_exit(self, market: str, holding: "dict | None",
                               price: float, df) -> "str | None":
        """보유 인버스 우선 청산 사유 — 강제청산 → 빠른익절 → EOD락인 → 반전락인 순 첫 발동 reason,
        없으면 None. 매도 실행(시장별 sell I/O)은 호출 핸들러가 한다.

        단축평가(앞 단계가 발동하면 뒷 단계는 호출 안 함) — 원래 핸들러의 순차 if-return 과 동일."""
        for check in (
            lambda: self._should_force_exit_inverse(),
            lambda: self._inverse_quick_profit_due(market, holding, price),
            lambda: self._inverse_eod_lock_due(market, holding, price),
            lambda: self._inverse_reversal_lock_due(market, holding, price, df),
        ):
            fired, reason = check()
            if fired:
                return reason
        return None

    def _inverse_leverage(self, code) -> float:
        """인버스 ETF 배율의 절대값. 메타 없으면 1.0. -1X→1.0, -2X→2.0, -3X→3.0."""
        if not code:
            return 1.0
        from zusik.analysis.smart_signals import SmartSignals
        meta = (SmartSignals.KR_INVERSE_ETF.get(str(code))
                or SmartSignals.US_INVERSE_ETF.get(str(code)) or {})
        return abs(float(meta.get("leverage", -1) or -1)) or 1.0

    def _inverse_entry_confirms(self, price, df, code=None) -> "tuple[bool, float]":
        """신규/증액 인버스 매수 확인 — 이 ETF의 상승이 '기초지수 급락'을 반영하는가.

        인버스는 기초지수가 내릴 때만 오른다. ETF '자기 등락률'을 레버리지로 정규화해
        '함의 지수 낙폭'을 구하고, 그게 급락 수준(기본 -2.5%, _fast_market_fall 지수 임계와
        정합)일 때만 통과한다. 단순 +1% 상승은 지수 -1%(강세장 노이즈)라 통과 못 함 →
        KOSPI만 빠질 때 나스닥/코스닥 인버스(409820/251340)까지 무차별 매수하던 손실 차단.
        -2X는 +5%, -3X는 +7.5% 있어야 같은 -2.5% 급락으로 확인(레버리지 정규화).
        df 부족하면 차단 안 함(기존 게이트에 위임).
        config: inverse.entry_index_drop_pct(기본 2.5%)."""
        idx_th = float((self.config.get("inverse", {}) or {}).get("entry_index_drop_pct", 2.5)) / 100.0
        if idx_th <= 0:
            return True, 0.0
        chg = self._intraday_change(price, df)
        if chg is None:
            return True, 0.0       # 데이터 부족 — 기존 동작 보존(차단하지 않음)
        implied_index_drop = chg / self._inverse_leverage(code)   # ETF +chg → 지수 -chg/lev
        return implied_index_drop >= idx_th, chg

    def _inverse_net_rate(self, market: str, holding: "dict | None", cur_price: float = 0.0) -> float:
        """보유 인버스의 왕복 수수료 공제 순익률 (EOD/반전 락인 공용)."""
        if not holding:
            return 0.0
        qty = float(holding.get("qty", 0) or 0)
        if qty <= 0:
            return 0.0
        avg = float(holding.get("avg_price", 0) or 0)
        cur = float(cur_price or holding.get("current_price", 0) or 0)
        from zusik.storage.portfolio_tracker import PortfolioTracker, FEE_RATES
        if avg > 0 and cur > 0:
            buy_fee = PortfolioTracker.estimate_fees(market, "buy", avg * qty)
            sell_fee = PortfolioTracker.estimate_fees(market, "sell", cur * qty)
            invest = avg * qty
            return (((cur - avg) * qty - buy_fee - sell_fee) / invest) if invest > 0 else 0.0
        gross = float(holding.get("profit_rate", 0) or 0) / 100.0
        return gross - (FEE_RATES.get(f"{market}_BUY", 0.0) + FEE_RATES.get(f"{market}_SELL", 0.0))

    def _inverse_quick_profit_due(self, market: str, holding: "dict | None",
                                  cur_price: float = 0.0) -> "tuple[bool, str]":
        """인버스 빠른 익절 — 순익이 임계(기본 1.5%) 이상이면 즉시 실현(작은 수익 확정).

        인버스는 시간이 갈수록 감쇠(decay)하고 지수는 우상향이라, 큰 하락을 기다리며 들고 있으면
        평가익이 되돌려져 손실로 끝나기 쉽다(KIS 2년: buy&hold -74~97%). 헷지가 통해 +1~2% 나면
        바로 챙기는 게 인버스에서 실제로 수익을 내는 현실적 방법(사용자 요청).
        임계는 고정이 아니라 inverse_take 사후데이터로 자가 보정된다(_learned_inverse_quick_profit).
        config: inverse.quick_profit_pct (seed, 0이면 비활성). 손실은 당연히 미발동(순익 기준)."""
        th = self._learned_inverse_quick_profit()
        if th <= 0 or not holding or float(holding.get("qty", 0) or 0) <= 0:
            return False, ""
        net = self._inverse_net_rate(market, holding, cur_price)
        if net < th:
            return False, ""
        return True, (f"인버스 빠른 익절 (순익 {net * 100:+.2f}% ≥ {th * 100:.1f}%) — 작은 수익 확정")

    def _inverse_reversal_lock_due(self, market: str, holding: "dict | None",
                                   price: float, df) -> "tuple[bool, str]":
        """헷지 성공 후 되돌림 즉시 락인 — 보유 인버스가 장중 하락(=기초지수 반등) + 순익이면
        EOD까지 안 기다리고 즉시 실현. '인버스를 제때 못 팔아' 장중 평가익이 증발하던 문제 대응.

        **손실 락인 금지**: 순익(net≥임계)일 때만 (수익 보호 장치는 손실 확정 안 함 —
        트레일링/crash 교훈). config: inverse.reversal_lock_pct(기본 1.5%) / eod_lock_min_profit."""
        cfg = (self.config.get("inverse", {}) or {})
        if not holding or float(holding.get("qty", 0) or 0) <= 0:
            return False, ""
        rev_th = float(cfg.get("reversal_lock_pct", 1.5)) / 100.0
        if rev_th <= 0:
            return False, ""
        chg = self._intraday_change(price, df)
        if chg is None or chg > -rev_th:    # 데이터 부족 또는 아직 안 빠짐(반등 약함) → 유지
            return False, ""
        net = self._inverse_net_rate(market, holding, price)
        if net < float(cfg.get("eod_lock_min_profit", 0.003)):
            return False, ""       # 손실/미미 → 락인 안 함
        return True, (f"인버스 반전 락인 (자기 {chg * 100:+.1f}% 되돌림=기초지수 반등, "
                      f"순익 {net * 100:+.2f}%) — 즉시 실현")

    def _inverse_under_max_ratio(self, code: str, balance: dict) -> bool:
        """KR 인버스 총노출이 config inverse.max_ratio 미만인가 (추가 헷지 매수 허용 여부)."""
        max_ratio = float(self.config.get("inverse", {}).get("max_ratio", 0.2))
        if max_ratio <= 0:
            return False
        try:
            total = int(balance.get("cash", 0) or 0) + int(balance.get("total_eval", 0) or 0)
            if total <= 0:
                return True
            inv_eval = sum(int(h.get("eval_amount", 0) or 0)
                           for h in balance.get("holdings", [])
                           if self._is_inverse(h.get("code", "")))
            return (inv_eval / total) < max_ratio
        except Exception:
            return True

    def _handle_inverse(self, code: str, name: str, price: int, df=None) -> None:
        """KR 인버스 ETF 전용 핸들러 — 모멘텀/RSI 분석기(급등=과매수=SELL 편향)를 우회하고
        헷지 로직으로 직접 판단한다.

        근본 원인: 인버스는 '헷지가 필요해 오른 바로 그 순간' 분석기가 과매수로 보고 SELL을
        내, 매수 경로 진입조차 못 했다(폭락일 인버스 매수 0건). 진입 게이트는 매수를 '허용'만
        할 뿐 '개시'하지 않으므로, 진짜 급락 시 분석기를 건너뛰고 직접 분할 매수한다.

        매도 정책(사용자 요청): 일반주와 동일 기준 적용. 횡보·급락도
        손절/트레일링/슬로우브리드로 대응. 이전엔 테일헷지 원칙으로 손절 0이었으나, 백테스트
        근거(buy&hold -74~97%) 대비 실전에서 평가손이 누적되는 문제 해결. 우선순위:
          1. 강제 청산 (peace 복귀 + bear<0.25) — 헷지 완료
          2. 급락 손절 — check_crash. hold_through 미적용(인버스는 보호 종목 아님)
          3. 급등 익절 — check_surge (+10%/+25%)
          4. 트레일링·본전 보호 — update_trailing_stop
          5. 느린 출혈 (횡보 대처) — slow_bleed, profit_rate<=-2% 시 매도
        진입은 _handle_buy(hedge_base_ratio=...)로 분할(0.35/0.30/0.35) 유지.
        """
        try:
            bal = self.client.get_balance()
        except Exception:
            return
        holding = next((h for h in bal.get("holdings", []) if h.get("code") == code), None)
        held_qty = int(holding.get("qty", 0) or 0) if holding else 0

        if held_qty > 0:
            # 우선 청산: 강제청산 → 빠른익절 → EOD락인 → 반전락인 (순수 판정, 첫 발동 매도)
            exit_reason = self._inverse_priority_exit("KR", holding, price, df)
            if exit_reason:
                logger.info("인버스 청산 %s: %s", name, exit_reason)
                self._handle_sell(code, name, force_reason=exit_reason)
                return

            if df is not None:
                # 2) 급락 손절 — 레짐기반, 사용자 승인): 인버스는 헷지다.
                # 지수 반등으로 인버스가 자기차트 -7%(crash_instant)를 찍는 건 '헷지 역할
                # 종료' 신호지 손절 사유가 아니다 → 얕은 당일급락은 무시하고 controlled
                # 강제청산(step1: peace+bear<0.25)에 위임. 깊은 붕괴(-15%↓/고점급락)만
                # 자본보호 하드스톱으로 컷. 반등일 인버스 crash_instant -66k 바닥투매 방지.
                crash = self.positions.check_crash(code, price, df)
                _deep = self._inverse_deep_collapse(crash)
                if crash and not _deep:
                    logger.info("인버스 얕은 급락 홀드(레짐위임): %s 당일 %+.1f%% — controlled 청산 대기",
                                name, crash.get("change", 0) * 100)
                elif _deep and not self._is_recently_bought(code, minutes=30):
                    logger.critical("인버스 깊은붕괴 강제손절: %s %+.1f%% → %s",
                                    name, crash.get("change", 0) * 100, crash.get("action", "손절"))
                    self._handle_sell(code, name,
                                      force_reason=f"인버스 강제 손절(깊은붕괴 -15%↓): {crash.get('reason', '')}",
                                      sell_ratio=crash.get("sell_ratio", 1.0))
                    return

                # 3) 급등 익절
                surge = self.positions.check_surge(code, price, df)
                if surge:
                    sell_ratio = surge.get("sell_ratio", 0.5)
                    logger.info("인버스 급등 익절 %s +%.0f%% → %s (ratio %.2f)",
                                name, surge.get("profit_rate", 0) * 100,
                                surge.get("action", "익절"), sell_ratio)
                    self._handle_sell(code, name,
                                      force_reason=surge.get("reason", "인버스 급등 익절"),
                                      sell_ratio=sell_ratio)
                    return

                # 4) 트레일링 스톱 + 본전 보호
                trailing = self.positions.update_trailing_stop(code, price)
                if trailing:
                    if trailing.get("action") == "stop_triggered":
                        logger.info("인버스 트레일링 스톱: %s 고점 %s → %s",
                                    name, f"{trailing['high']:,}", f"{price:,}")
                        self._handle_sell(code, name, force_reason="인버스 트레일링 스톱")
                        return
                    if trailing.get("action") == "breakeven_protect":
                        peak = trailing.get("peak_profit", 0) * 100
                        logger.info("인버스 본전 보호: %s 최고 +%.1f%% → %s, 수익 소멸 방지",
                                    name, peak, f"{price:,}")
                        self._handle_sell(code, name,
                                          force_reason=f"인버스 본전 보호 (최고 +{peak:.1f}%)")
                        return

                # 5) 느린 출혈 (횡보 대처) — 매수 직후 30분 보호
                if not self._is_recently_bought(code, minutes=30):
                    try:
                        from zusik.analysis.indicators import slow_bleed
                        bleed = slow_bleed(df, lookback=5, min_down_ratio=0.65, min_cum_drop=0.025)
                        if bleed.get("is_bleeding"):
                            pr = holding.get("profit_rate", 0) if holding else 0
                            if pr <= -2.0:
                                logger.warning("인버스 느린 출혈: %s 5일 누적 %+.1f%%, 수익률 %+.1f%% → 매도",
                                               name, bleed.get("cum_drop", 0) * 100, pr)
                                self._handle_sell(code, name,
                                                  force_reason=f"인버스 느린 출혈 (5일 {bleed.get('cum_drop',0):+.1%})")
                                return
                    except Exception:
                        pass

        allow, reason = self._should_allow_inverse_entry()
        if not allow:
            if held_qty > 0:
                logger.info("인버스 보유 유지 %s: %s", name, reason)
            return
        # 시장별 무차별 매수 차단: 이 인버스의 상승이 '기초지수 급락' 수준일 때만 매수.
        # KOSPI만 빠질 때 나스닥/코스닥 인버스까지 한꺼번에 사는 손실 방지(레버리지 정규화).
        confirm, chg = self._inverse_entry_confirms(price, df, code)
        if not confirm:
            logger.info("인버스 매수 보류 %s: 자기 등락 %+.2f%% — 함의 지수 낙폭이 급락 미달(무차별 매수 차단)",
                        name, chg * 100)
            return
        # 헷지 증액 시간 간격: 1차는 즉시(빠른 대응), 2차+는 직전 헷지 매수 후
        # 30분 경과 시에만. 시초 whipsaw 실측: 09:04 감지 → 09:06~16 10분에 전 차수
        # 소진(인버스 +3.7% 스파이크 추격) → 코스닥 V반등 +2.75% → 헷지 -2.4~-4.8%.
        # cooldown 완전 우회가 진자를 반대로 보냄 — 시간 사다리로 중간점 복원.
        # 급락이 30분+ 지속되면(진짜 폭락) 증액은 그대로 진행된다.
        if held_qty > 0 and self._is_recently_bought(code, minutes=30):
            logger.info("인버스 증액 보류 %s: 직전 헷지 매수 30분 미경과 — 급락 지속 확인 대기", name)
            return
        if not self._inverse_under_max_ratio(code, bal):
            logger.info("인버스 매수 보류 %s: max_ratio 한도 도달", name)
            return
        hedge_ratio = float(self.config.get("inverse", {}).get("max_ratio", 0.2))
        logger.info("인버스 선제 헷지 매수 %s: %s", name, reason)
        self._handle_buy(code, name, price, df, hedge_base_ratio=hedge_ratio)

