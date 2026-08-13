from __future__ import annotations

import logging
from datetime import datetime, timedelta


from zusik.analysis.smart_signals import SmartSignals


logger = logging.getLogger(__name__)


class USTradingMixin:
    """USTradingMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _inverse_us_tickers(self) -> set[str]:
        return set(SmartSignals.US_INVERSE_ETF.keys())

    def _inverse_us_list(self) -> list[dict]:
        if not getattr(self, "derivative_etf_enabled", True):
            return []
        return [{"ticker": t, "name": meta["name"], "exchange": meta.get("exchange", "NASD")}
                for t, meta in SmartSignals.US_INVERSE_ETF.items() if meta.get("default")]

    def _inverse_under_max_ratio_us(self, ticker: str, us_balance: dict) -> bool:
        """US 인버스 총노출(USD)이 config inverse.max_ratio 미만인가."""
        max_ratio = float(self.config.get("inverse", {}).get("max_ratio", 0.2))
        if max_ratio <= 0:
            return False
        try:
            holdings = us_balance.get("holdings", [])
            total = float(us_balance.get("cash_usd", 0) or 0) + \
                sum(float(h.get("eval_amount", 0) or 0) for h in holdings)
            if total <= 0:
                return True
            inv_eval = sum(float(h.get("eval_amount", 0) or 0)
                           for h in holdings if self._is_inverse(h.get("ticker", "")))
            return (inv_eval / total) < max_ratio
        except Exception:
            return True

    def _handle_inverse_us(self, ticker: str, name: str, exchange: str,
                           df, us_balance: dict, holding: dict | None) -> None:
        """US 인버스 ETF 전용 헷지 핸들러 (KR _handle_inverse의 US 버전)."""
        try:
            price_info = self.client.get_us_current_price(ticker, exchange)
            price = float(price_info.get("price", 0) or 0)
        except Exception:
            return
        if price <= 0:
            return
        held_qty = int(holding.get("qty", 0) or 0) if holding else 0

        if held_qty > 0:
            # 우선 청산: 강제청산 → 빠른익절 → EOD락인 → 반전락인 (순수 판정, 첫 발동 전량매도)
            exit_reason = self._inverse_priority_exit("US", holding, price, df)
            if exit_reason:
                logger.info("US 인버스 청산 %s: %s", name, exit_reason)
                self._us_force_sell_reason = exit_reason  # 전량 강제매도 + reason 태깅 경로
                try:
                    self._handle_us_sell(ticker, name, exchange, df=df)
                finally:
                    self._us_force_sell_reason = None
                return

            if df is not None:
                # 2) 급락 손절 — 레짐기반, KR _handle_inverse와 동일 정책):
                # 얕은 당일급락(crash_instant)은 헷지 역할종료 신호 → controlled 청산에 위임.
                # 깊은 붕괴(-15%↓/고점급락)만 자본보호 하드스톱 컷.
                crash = self.positions.check_crash(ticker, price, df)
                _deep = self._inverse_deep_collapse(crash)
                if crash and not _deep:
                    logger.info("US 인버스 얕은 급락 홀드(레짐위임): %s 당일 %+.1f%% — controlled 청산 대기",
                                name, crash.get("change", 0) * 100)
                elif _deep and not self._is_recently_bought(ticker, minutes=30):
                    logger.critical("US 인버스 깊은붕괴 강제손절: %s %+.1f%% → %s",
                                    name, crash.get("change", 0) * 100, crash.get("action", "손절"))
                    self._handle_us_sell(ticker, name, exchange, df=df)
                    return

                # 3) 급등 익절
                surge = self.positions.check_surge(ticker, price, df)
                if surge:
                    logger.info("US 인버스 급등 익절 %s +%.0f%%",
                                name, surge.get("profit_rate", 0) * 100)
                    self._handle_us_sell(ticker, name, exchange, df=df)
                    return

                # 4) 트레일링 + 본전 보호
                trailing = self.positions.update_trailing_stop(ticker, int(price))
                if trailing:
                    if trailing.get("action") == "stop_triggered":
                        logger.info("US 인버스 트레일링 스톱: %s 고점 %s → %s",
                                    name, f"{trailing['high']:,}", f"{price:,.2f}")
                        self._handle_us_sell(ticker, name, exchange, df=df)
                        return
                    if trailing.get("action") == "breakeven_protect":
                        peak = trailing.get("peak_profit", 0) * 100
                        logger.info("US 인버스 본전 보호: %s 최고 +%.1f%% → $%.2f",
                                    name, peak, price)
                        self._handle_us_sell(ticker, name, exchange, df=df)
                        return

                # 5) 느린 출혈 (횡보 대처)
                if not self._is_recently_bought(ticker, minutes=30):
                    try:
                        from zusik.analysis.indicators import slow_bleed
                        bleed = slow_bleed(df, lookback=5, min_down_ratio=0.65, min_cum_drop=0.025)
                        if bleed.get("is_bleeding"):
                            pr = holding.get("profit_rate", 0) if holding else 0
                            if pr <= -2.0:
                                logger.warning("US 인버스 느린 출혈: %s 5일 %+.1f%%, 수익률 %+.1f%% → 매도",
                                               name, bleed.get("cum_drop", 0) * 100, pr)
                                self._handle_us_sell(ticker, name, exchange, df=df)
                                return
                    except Exception:
                        pass

        allow, reason = self._should_allow_inverse_entry()
        if not allow:
            if held_qty > 0:
                logger.info("US 인버스 보유 유지 %s: %s", name, reason)
            return
        # 시장별 무차별 매수 차단: 이 인버스가 장중 상승(=기초지수 하락) 확인일 때만 매수
        confirm, chg = self._inverse_entry_confirms(price, df)
        if not confirm:
            logger.info("US 인버스 매수 보류 %s: 자기 등락 %+.2f%% — 이 시장 지금 안 빠짐(무차별 매수 차단)",
                        name, chg * 100)
            return
        # 헷지 증액 시간 간격, KR과 동일): 1차 즉시, 2차+는 30분 경과 시에만
        if held_qty > 0 and self._is_recently_bought(ticker, minutes=30):
            logger.info("US 인버스 증액 보류 %s: 직전 헷지 매수 30분 미경과", name)
            return
        if not self._inverse_under_max_ratio_us(ticker, us_balance):
            logger.info("US 인버스 매수 보류 %s: max_ratio 한도 도달", name)
            return
        hedge_ratio = float(self.config.get("inverse", {}).get("max_ratio", 0.2))
        logger.info("US 인버스 선제 헷지 매수 %s: %s", name, reason)
        self._handle_us_buy(ticker, name, price, exchange, df, hedge_base_ratio=hedge_ratio)

    def _rotate_one_us_to_index(self, plan: dict) -> bool:
        """US 인덱스 회전 (KR 패턴과 동일)."""
        if not self.client.is_us_market_open():
            return False
        us_bal = self.client.get_us_balance()
        cash_usd = float(us_bal.get("cash_usd", 0) or 0)
        us_total_usd = max(cash_usd + float(us_bal.get("us_eval_usd", 0) or 0), 0.01)
        cash_ratio = cash_usd / us_total_usd
        target_ticker = "SPY"
        target_exchange, target_name = self._INDEX_ETF_US[target_ticker]

        if cash_ratio >= 0.10 and cash_usd >= max(self.min_amount_usd, 5):
            try:
                price_info = self.client.get_us_current_price(target_ticker, target_exchange)
                price = float(price_info.get("price", 0) or 0)
                if price <= 0:
                    return False
                df = None
                try:
                    df = self.client.get_us_ohlcv(target_ticker, exchange=target_exchange)
                except Exception:
                    pass
                logger.info("인덱스 회전(US): bull=%.2f, alloc=%.0f%% < %.0f%% → %s 매수",
                            plan["bull"], plan["alloc"]["us"] * 100, plan["min_us"] * 100, target_ticker)
                if self.discord:
                    try:
                        self.discord.notify_info(
                            f"상승장 회전 (bull {plan['bull']:.2f}) — "
                            f"S&P 노출 {plan['alloc']['us']*100:.0f}% < {plan['min_us']*100:.0f}% "
                            f"→ {target_ticker} 매수 시도"
                        )
                    except Exception:
                        pass
                self._handle_us_buy(target_ticker, target_name, price, target_exchange, df=df)
                return True
            except Exception:
                logger.debug("US 인덱스 매수 실패", exc_info=True)
                return False

        # 현금 부족 — KR과 동일 패턴, 가장 약한 비인덱스/비인버스 매도
        from zusik.analysis.indicators import momentum_score as _mom
        weakest = None
        weakest_score = 999.0
        for h in us_bal.get("holdings", []) or []:
            tk = h.get("ticker")
            qty = int(h.get("qty", 0) or 0)
            if not tk or qty <= 0:
                continue
            if tk in self._INDEX_ETF_US or self._is_inverse(tk):
                continue
            try:
                df_h = self.client.get_us_ohlcv(tk, exchange=h.get("exchange", "NASD"))
                if df_h is None or len(df_h) < 10:
                    continue
                m = _mom(df_h)
                if m < weakest_score:
                    weakest_score = m
                    weakest = h
            except Exception:
                continue

        if not weakest:
            return False

        tk = weakest["ticker"]
        name = weakest.get("name", tk)
        qty = int(weakest.get("qty", 0) or 0)
        cur_price = float(weakest.get("current_price", 0) or 0)
        avg_price = float(weakest.get("avg_price", 0) or 0)
        exch = weakest.get("exchange", "NASD")
        logger.info("인덱스 회전(US): bull=%.2f, %s 약세 mom=%.2f → 매도",
                    plan["bull"], tk, weakest_score)
        if self.discord:
            try:
                self.discord.notify_info(
                    f"상승장 회전: {name}({tk}) 약세 (mom {weakest_score:+.2f}) 매도 "
                    f"→ 다음 사이클 SPY 매수"
                )
            except Exception:
                pass
        result = self.client.sell_us_market(tk, qty, exch)
        if result.get("success"):
            pnl_info = {"realized_pnl": 0, "realized_rate": 0}
            try:
                fx_now = self.client.get_usd_krw_rate()
                cur_krw = int(cur_price * fx_now)
                avg_krw = int(avg_price * fx_now)
                pnl_info = self.tracker.record_sell(
                    tk, name, qty, cur_krw, avg_krw,
                    reason="상승장 회전 — 인덱스 노출 확대 위해 약세 종목 청산",
                ) or pnl_info
            except Exception:
                logger.debug("회전 매도 기록 실패", exc_info=True)
            try:
                self.positions.record_sell(tk, qty)
            except Exception:
                pass
            if self.discord:
                try:
                    self.discord.notify_trade(
                        side="sell", stock_name=name, stock_code=tk,
                        qty=qty, price=cur_price,
                        reason="상승장 회전 (US 인덱스 노출 확대)",
                        realized_pnl=pnl_info.get("realized_pnl", 0),
                        realized_rate=pnl_info.get("realized_rate", 0),
                    )
                except Exception:
                    logger.debug("회전 매도 Discord 알림 실패", exc_info=True)
            return True
        return False

    def _leftover_momentum_ok(self, df) -> bool:
        """잔여 달러 소진이 '양의 모멘텀'을 요구하는 단일 게이트.

        momentum_score(df)(-1~+1) >= position.leftover_momentum_min(기본 0.10) 이면 True.
        떨어지진 않지만 오르지도 않는(평평/식은) 종목에 자투리 현금을 넣어 죽은 자본이 되던
        문제(실증: MSFT 1주 peak +0.24% → -12.8%) 방지. 미달이면 매수 안 하고 현금을 둔다.
        데이터 부족/예외는 보수적으로 False(매수 보류).
        """
        try:
            from zusik.analysis.indicators import momentum_score
            mom_min = float((self.config.get("position", {}) or {}).get("leftover_momentum_min", 0.10))
            return float(momentum_score(df)) >= mom_min
        except Exception:
            return False

    def _execute_us_stock(self, stock: dict):
        """미국 주식 단일 종목 분석 → 매매."""
        ticker = stock["ticker"]
        name = stock.get("name") or ticker
        exchange = stock.get("exchange", "NASD")

        # ── 현금 사전 체크: 미보유 + 달러 예수금 부족 → 분석 스킵 ──
        try:
            us_bal_quick = self.client.get_us_balance()
            holding_now = next((h for h in us_bal_quick.get("holdings", []) if h["ticker"] == ticker), None)
            if not holding_now:
                cash_usd = us_bal_quick.get("cash_usd", 0.0)
                cur_price_info = self.client.get_us_current_price(ticker, exchange)
                cur_price_usd = cur_price_info.get("price", 0)
                if cash_usd < max(self.min_amount_usd, cur_price_usd):
                    logger.debug("US %s 분석 스킵 — 미보유 + 달러 부족 ($%.2f < max(%s, $%.2f))",
                                 name, cash_usd, self.min_amount_usd, cur_price_usd)
                    return
        except Exception:
            pass

        logger.info("─── US %s(%s) ───", name, ticker)

        try:
            df = self.client.get_us_ohlcv(ticker, exchange=exchange, period=self.period)
        except Exception as e:
            logger.warning("US %s 시세 조회 실패 (해외주식 서비스/달러 확인): %s", ticker, str(e)[:80])
            return
        if df is None or df.empty:
            logger.warning("%s 데이터 없음", ticker)
            return

        # 시장 전체 crisis는 detect_market_condition이 담당. 개별 종목 df로는 판정 안 함.
        # 종목별 급락이 전체 긴급 홀딩을 유발하는 무한 루프 버그 방지.
        if self.risk.is_emergency_hold():
            logger.info("%s — 긴급 홀딩, 건너뜀", name)
            return

        # ── 잔고 조회 (분석 전에 보유/예수금 상황 파악) ──
        try:
            us_balance = self.client.get_us_balance()
        except Exception:
            us_balance = {"cash_usd": 0.0, "holdings": []}
        us_holdings = us_balance.get("holdings", [])
        cash_usd = us_balance.get("cash_usd", 0.0)
        holding = next((h for h in us_holdings if h["ticker"] == ticker), None)

        # ── 인버스 ETF는 전용 헷지 핸들러로 라우팅 ──
        # 분석기 우회 (급등 인버스를 과매수=SELL로 오판 → 매수 경로 차단되는 문제). KR과 동일.
        if self._is_inverse(ticker):
            self._handle_inverse_us(ticker, name, exchange, df, us_balance, holding)
            return

        # ── 보유 중일 때 로컬 안전장치 (API 비용 $0) ──
        if holding and holding.get("qty", 0) > 0 and len(df) >= 2:
            holding["qty"]
            profit_rate = holding.get("profit_rate", 0) / 100
            curr_close = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            daily_change = (curr_close - prev_close) / prev_close if prev_close > 0 else 0

            # 느린 출혈 감지 (US —: -3% → -2% 누적으로 조기 감지)
            from zusik.analysis.indicators import slow_bleed
            bleed = slow_bleed(df, lookback=5, min_down_ratio=0.55, min_cum_drop=0.02)

            # 본전 보호 + high_since_buy 추적 (매수 이후 고점만 기준)
            pos_state = self.positions._get_position(ticker) if self.positions.has_position(ticker) else {}
            avg_price = holding.get("avg_price", 0) or curr_close
            high_since_buy = max(pos_state.get("high_since_buy", avg_price), curr_close, avg_price)
            peak_profit = max(pos_state.get("peak_profit_rate", 0), profit_rate)
            if self.positions.has_position(ticker):
                pos_state["peak_profit_rate"] = peak_profit
                pos_state["high_since_buy"] = high_since_buy
                self.positions._positions[ticker] = pos_state
                from zusik.core.position_manager import _save_positions
                _save_positions(self.positions._positions)
            # 매수 이후 고점 대비 하락률 (df 전체 고점이 아닌 보유 이후만)
            from_high = (curr_close - high_since_buy) / high_since_buy if high_since_buy > 0 else 0

            # 로컬 빠른 트리거 (5/1 추가): 변동성 + 시장 자동 분류 → 일봉/5분/1분/틱
            import zusik.core.volatility_classifier as vc
            tier_info_us = vc.classify(
                df,
                market_condition=getattr(self, "_market_condition", "peace"),
                holding=True,
            )
            logger.debug("US %s 변동성: %s", name, tier_info_us["reason"])

            rsi_exit_us = self.signals.check_overbought_exit(df, profit_rate=profit_rate, rsi_min=self._adaptive_params().get("rsi_exit_min", 80), profit_min=self._adaptive_params().get("rsi_exit_profit_min", 0.03))
            quick_loss_us = self.signals.check_quick_loss_exit(df, profit_rate=profit_rate)

            # 5분봉: medium 이상
            if not rsi_exit_us and not quick_loss_us and tier_info_us["use_minute_5"]:
                try:
                    m5 = self.client.get_us_minute_ohlcv(ticker, exchange, minutes=5)
                    if m5 is not None and len(m5) >= 21:
                        rsi_exit_us = self.signals.check_overbought_exit(m5, profit_rate=profit_rate, rsi_min=self._adaptive_params().get("rsi_exit_min", 80), profit_min=self._adaptive_params().get("rsi_exit_profit_min", 0.03))
                        if rsi_exit_us:
                            rsi_exit_us["reason"] = "[5분봉] " + rsi_exit_us["reason"]
                        else:
                            quick_loss_us = self.signals.check_quick_loss_exit(m5, profit_rate=profit_rate)
                            if quick_loss_us:
                                quick_loss_us["reason"] = "[5분봉] " + quick_loss_us["reason"]
                except Exception:
                    pass
            # 1분봉: high 이상
            if not rsi_exit_us and not quick_loss_us and tier_info_us["use_minute_1"]:
                try:
                    m1 = self.client.get_us_minute_ohlcv(ticker, exchange, minutes=1)
                    if m1 is not None and len(m1) >= 21:
                        rsi_exit_us = self.signals.check_overbought_exit(m1, profit_rate=profit_rate, rsi_min=self._adaptive_params().get("rsi_exit_min", 80), profit_min=self._adaptive_params().get("rsi_exit_profit_min", 0.03))
                        if rsi_exit_us:
                            rsi_exit_us["reason"] = "[1분봉] " + rsi_exit_us["reason"]
                        else:
                            quick_loss_us = self.signals.check_quick_loss_exit(m1, profit_rate=profit_rate)
                            if quick_loss_us:
                                quick_loss_us["reason"] = "[1분봉] " + quick_loss_us["reason"]
                except Exception:
                    pass
            # WebSocket: extreme tier
            if tier_info_us["use_websocket"]:
                self._ensure_ws_subscription(ticker, market="US", exchange=exchange)

            force_reason = None
            us_sell_ratio = 1.0  # RSI 트림 시 0.5, 그 외 전량
            # 매수 직후 30분 보호 — RIOT 4/29 buy→sell 1분 churn 방지.
            # 방금 산 종목을 -7% 급락 trigger로 즉시 매도하면 손실 확정 후 잔여 소진 매수가 또 사 무한루프
            recently_bought = self._is_recently_bought(ticker, minutes=30)
            if (daily_change <= -0.07 and not recently_bought
                    and not self._hold_through_loss(ticker, profit_rate)):
                # 조기손절 억제: pullback 구간(pr>floor)이면 보류, 하드스톱/트레일링이 처리.
                force_reason = f"당일 {daily_change:+.1%} 급락 — 추가 하락 방지 전량 매도"
            elif daily_change <= -0.07 and recently_bought:
                logger.warning("US %s 당일 %+.1f%% 급락이지만 매수 직후 30분 — 매도 보류",
                               name, daily_change * 100)
            elif self._trailing_fire_allowed(from_high, profit_rate):
                #: 수익 구간에서만 발동 — 트레일링=수익보호 장치, 손실 확정 금지.
                # 이 인라인 트레일링이 수익률 무관 발동해 실측 2건 전패 -264k (델 -76k
                # @손실, HPE -187k @-8.4%). 손실 구간은 hold floor/하드스톱(-15%)이 담당.
                force_reason = f"최근 고점 대비 {from_high:+.1%} 하락 — 트레일링 스톱"
            elif from_high <= -0.10 and not self._hold_through_loss(ticker, profit_rate):
                # floor(-9%/-15%) 아래로 뚫린 경우만 손절 경로로 — 라벨도 손절로 정직하게.
                force_reason = f"손절 (고점 {from_high:+.1%}, 손익 {profit_rate:+.1%} — floor 이탈)"
            elif profit_rate <= -0.15:
                force_reason = f"손절선 도달 ({profit_rate:+.1%}) — 강제 매도"
            elif (self.positions.breakeven_should_protect(
                        peak_profit, profit_rate, pos_state.get("rsi_trimmed"))
                    and not self._core_hold_through(ticker)):
                # 본전 보호 = 피크 비례 보존: KR(update_trailing_stop)과 동일
                # 단일 소스(breakeven_should_protect). 피크 +8% → +5.5%에서 잠가 고점 반납 cap.
                # 핵심주는 면제(KR과 동일, churn 방지 — 큰 추세는 트레일링/surge가 처리).
                _floor = self.positions.breakeven_protect_floor(peak_profit)
                force_reason = (f"본전 보호 — 최고 +{peak_profit*100:.1f}% → "
                                f"보존바닥 +{_floor*100:.1f}% 도달 익절")
            elif rsi_exit_us:
                # RSI 과매수 빠른 익절 (LLM 없이 즉시) — 100% 승률 패턴 재현.
                # 트림: 에피소드당 절반 익절 + 절반 라이딩 (적시성 27% 개선)
                _trim = self._rsi_trim_ratio(ticker)
                if _trim is None:
                    logger.info("US RSI 과매수 유지 — 트림 완료, 잔여 라이딩: %s", name)
                else:
                    force_reason = rsi_exit_us["reason"]
                    us_sell_ratio = _trim
            elif (quick_loss_us and not recently_bought
                    and not self._hold_through_loss(ticker, profit_rate)):
                # 단기 약세 누적 빠른 손절.: 조기손절 억제 floor 적용.
                force_reason = quick_loss_us["reason"]
            elif (bleed["is_bleeding"] and profit_rate <= -0.03 and not recently_bought
                    and not self._hold_through_loss(ticker, profit_rate)):
                # 조기 손절 임계 완화: -1.5% → -3%로 보수화.
                # 매수 직후 30분 보호 적용. NIO -1,402 / GRAB -838 / RIOT churn 사례에서
                # 너무 빠른 -1.5% 컷이 반등 가능 종목까지 매도해 손실 누적시켰음.
                force_reason = (
                    f"느린 출혈 — 5일 중 {bleed['down_ratio']*100:.0f}% 음봉, "
                    f"누적 {bleed['cum_drop']:+.1%}, 저점 갱신 {bleed['lower_lows']}회"
                )
            elif (not recently_bought
                    and self._check_stale_rotate(ticker, profit_rate, df)):
                # 회전 청산: 72h+ 모멘텀 소멸 + 본전 회복 — 강세에 팔아 재배치
                force_reason = "회전 청산 — 72h+ 모멘텀 소멸, 본전 회복 자본 재배치"

            if force_reason:
                logger.critical("US 긴급 매도: %s — %s (ratio %.2f)", name, force_reason, us_sell_ratio)
                self._us_force_sell_reason = f"{force_reason}"
                self._handle_us_sell(ticker, name, exchange, sell_ratio=us_sell_ratio)
                self._us_force_sell_reason = None
                if us_sell_ratio < 1.0:
                    self.positions.mark_rsi_trimmed(ticker)  # 잔여 라이딩분 본전보호 개시
                return

        # ── 총자산 계산 (KR + US 환산, KIS 실시간 FX) ──
        try:
            kr_bal = self.client.get_balance()
            kr_cash = kr_bal.get("cash", 0)
            kr_stock_value = sum(h.get("eval_amount", h.get("current_price", 0) * h.get("qty", 0))
                                 for h in kr_bal.get("holdings", []))
        except Exception:
            kr_cash = 0
            kr_stock_value = 0
        us_stock_value_usd = sum(h.get("eval_amount", 0) for h in us_holdings)
        fx = self.client.get_usd_krw_rate()
        total_krw = kr_cash + kr_stock_value + int((cash_usd + us_stock_value_usd) * fx)
        total_info = (
            f"총자산 ≈ {total_krw:,}원 "
            f"(KR 현금 {kr_cash:,}원 + KR 주식 {kr_stock_value:,}원 + "
            f"US 현금 ${cash_usd:.2f} + US 주식 ${us_stock_value_usd:.2f}, FX={fx:,.2f})"
        )

        # ── Claude 분석 컨텍스트: 보유종목 + 달러잔고 + 장기투자 + 성과 ──
        if self.use_claude:
            self.strategy.set_stock(ticker, name)
            if not us_holdings:
                holdings_text = (
                    f"현재 미국 보유 0종목. 달러 예수금 ${cash_usd:.2f}. "
                    "매수/관망 중립적으로 판단하세요."
                )
            else:
                items = [
                    f"{h.get('name') or h['ticker']} {h['qty']}주({h.get('profit_rate', 0):+.1f}%)"
                    for h in us_holdings
                ]
                holdings_text = f"미국 보유: {', '.join(items)}. 달러 예수금: ${cash_usd:.2f}."

            perf_info = self.reward.get_performance_summary_text()

            # 모멘텀 돌파 힌트 (US)
            from zusik.analysis.indicators import breakout_signal, volume_surge, momentum_score
            bk = breakout_signal(df, 20)
            vs = volume_surge(df, 20, 2.0)
            mom = momentum_score(df)
            breakout_hint = ""
            if bk["is_breakout"] and vs["is_surge"]:
                breakout_hint = (
                    f"20일 고점 돌파({bk['distance_pct']:+.1%}) + 거래량 {vs['ratio']:.1f}배 "
                    f"+ 모멘텀 {mom:+.2f} → 추세 시작 후보"
                )
            elif bk["is_breakout"]:
                breakout_hint = f"20일 고점 돌파({bk['distance_pct']:+.1%}, 모멘텀 {mom:+.2f})"

            portfolio_info = f"(US) {holdings_text} | {total_info}"
            if perf_info:
                portfolio_info += f" | {perf_info}"
            if breakout_hint:
                portfolio_info += f" | {breakout_hint}"

            lt_holdings = self.tracker.get_long_term_holdings()
            lt_info = ""
            if lt_holdings:
                lt_total = self.tracker.get_long_term_total_cost()
                lt_info = f"장기투자 {len(lt_holdings)}종목, {lt_total:,}원"

            # MC 통계 — LLM 분석에 통계적 근거 제공 (US도 동일)
            mc_stats_us = self._compute_mc_stats(df, n_paths=10000, t_forward=30)
            self._last_mc_stats = mc_stats_us
            mc_info_us = self._format_mc_for_llm(mc_stats_us) if mc_stats_us else ""
            try:
                self.strategy.set_context(portfolio_info=portfolio_info,
                                          long_term_info=lt_info,
                                          mc_info=mc_info_us)
            except TypeError:
                self.strategy.set_context(portfolio_info=portfolio_info,
                                          long_term_info=lt_info)

            # auto_hybrid에 포지션 상태 주입 (손실이면 재분석 주기 단축)
            if hasattr(self.strategy, "set_position_state"):
                from zusik.analysis.indicators import slow_bleed as _slow_bleed
                pr = (holding.get("profit_rate", 0) / 100) if holding else 0.0
                pos = self.positions._get_position(ticker) if self.positions.has_position(ticker) else {}
                bleed_info = _slow_bleed(df) if holding else {"is_bleeding": False}
                self.strategy.set_position_state(
                    holding=bool(holding),
                    profit_rate=pr,
                    is_bleeding=bleed_info["is_bleeding"],
                    peak_profit=pos.get("peak_profit_rate", 0.0),
                )

        signal = self.strategy.analyze(df)

        price_info = self.client.get_us_current_price(ticker, exchange)
        price = price_info["price"]
        intraday_change_us = (price_info.get("change_rate", 0) or 0) / 100
        self._last_intraday_change[ticker] = intraday_change_us  # 매수 게이트 참조

        # 신호 진동 가드 — 같은 종목 6h 내 BUY ↔ SELL 뒤집힘 차단
        _hyst_conf_us = 0.5
        if self.use_claude:
            _hyst_analysis = self.strategy.get_last_analysis() or {}
            _hyst_conf_us = float(_hyst_analysis.get("confidence", 0.5) or 0.5)
        _eff_sig_us, _hyst_msg_us = self._apply_hysteresis(ticker, signal, _hyst_conf_us)
        if _eff_sig_us != signal:
            logger.info("US %s 신호 가드: %s", name, _hyst_msg_us)
            signal = _eff_sig_us

        label = {"buy": "매수", "long_term_buy": "장기매수", "sell": "매도", "hold": "관망"}.get(signal, signal)
        logger.info("%s $%.2f (%+.2f%%) -> %s", name, price, price_info["change_rate"], label)

        # 인버스 강제 청산: 평시 복귀 + 하락 완화 시 전략 신호 무관하게 즉시 매도
        if holding and holding.get("qty", 0) > 0 and self._is_inverse(ticker):
            force_exit, exit_reason = self._should_force_exit_inverse()
            if force_exit:
                logger.info("US 인버스 강제 청산 %s: %s", name, exit_reason)
                self._handle_us_sell(ticker, name, exchange)
                return

        # 핵심(whitelist) 코어 진입 (US) — KR과 동일. hold/약매도 신호여도 미보유 핵심주
        # (NVDA/AAPL/MSFT)를 비방어·비하락장·비과열에서 conviction 하한만큼 확보.
        _pos_cfg_wlu = self.config.get("position", {}) or {}
        _held_us = bool(holding and holding.get("qty", 0) > 0)
        if (signal not in ("buy", "long_term_buy")
                and _pos_cfg_wlu.get("whitelist_core_entry", True)
                and self._is_whitelist(ticker) and not _held_us
                and not self._is_inverse(ticker)
                and not getattr(self, "_defensive_mode", False)):
            try:
                _bear_wlu = self._bearish_regime_score()
            except Exception:
                _bear_wlu = 0.0
            _max_intraday_u = float(_pos_cfg_wlu.get("whitelist_core_max_intraday", 0.08))
            try:
                _cash_u = self.client.get_us_balance().get("cash_usd", 0)
            except Exception:
                _cash_u = 0
            if _bear_wlu >= 0.5:
                logger.info("US 핵심주 %s 코어 진입 보류 — 하락국면(bear %.2f)", name, _bear_wlu)
            elif intraday_change_us >= _max_intraday_u:
                logger.info("US 핵심주 %s 코어 진입 보류 — 과열(장중 %+.1f%%)", name, intraday_change_us * 100)
            elif _cash_u >= price > 0:
                logger.info("US 핵심주 코어 진입: %s (%s·미보유 → conviction 하한 매수, 장중 %+.1f%%)",
                            name, signal, intraday_change_us * 100)
                self._handle_us_buy(ticker, name, price, exchange, df, is_long_term=False)
                return

        if signal in ("buy", "long_term_buy"):
            self._handle_us_buy(ticker, name, price, exchange, df, is_long_term=(signal == "long_term_buy"))
        elif signal == "sell":
            if holding and holding.get("qty", 0) > 0:
                _sell_conf_us = 1.0
                if self.use_claude:
                    _sa = self.strategy.get_last_analysis() or {}
                    _sell_conf_us = float(_sa.get("confidence", 1.0) or 1.0)
                defer, reason = self._should_defer_sell(
                    "US", df,
                    holding["qty"], holding.get("avg_price", 0), float(curr_close),
                    confidence=_sell_conf_us,
                )
                # 모호(pop-then-fade) 익절 구간이면 LLM 타이브레이크로 연기 해제 가능
                defer, reason = self._resolve_ambiguous_sell(
                    "US", df, ticker, name,
                    holding["qty"], holding.get("avg_price", 0),
                    float(curr_close), defer, reason,
                )
                if defer:
                    logger.info("%s SELL 연기: %s", name, reason)
                else:
                    logger.info("%s SELL 확정: %s", name, reason)
                    # 모호익절 오버라이드면 reason을 force로 넘겨 EOD 패턴(ambiguous_take) 측정
                    if "모호판정" in (reason or ""):
                        self._us_force_sell_reason = reason
                    try:
                        self._handle_us_sell(ticker, name, exchange, df=df)
                    finally:
                        self._us_force_sell_reason = ""
            else:
                self._handle_us_sell(ticker, name, exchange, df=df)

        # ── 잔여 달러 소진: 매수 후 남은 $가 최소금액 이상이면 다른 종목 1주라도 ──
        #: 매수 gate (장전 sentiment / defensive / drawdown) 우회 차단
        #: 재진입 차단/일일 매도 한도까지 우회하던 버그 수정.
        # 원인: RIOT 4/29 7회 buy→sell 루프 — 잔여 소진이 churn_guard를 거치지 않아 24시간 차단된 종목을 즉시 재매수
        #: 양의 모멘텀 요구(leftover_momentum_min) — 평평/식은 종목에 자투리 현금을 넣어
        #  죽은 자본이 되던 문제(MSFT peak +0.24% → -12.8%) 방지. 미달이면 현금을 그대로 둔다.
        if not (self.config.get("cash_deployment", {}) or {}).get(
                "allow_leftover_orders", True):
            return
        try:
            # 장전 sentiment & defensive 게이트 통과해야 잔여 소진 매수도 허용
            analysis = self.strategy.get_last_analysis() if self.use_claude else None
            conf = (analysis or {}).get("confidence", 0.5) if analysis else 0.5
            allow_pm, pm_reason = self._pre_market_buy_gate("US", conf, symbol=ticker)
            if not allow_pm:
                logger.info("US 잔여 소진 차단 (%s): %s", name, pm_reason)
                return
            if getattr(self, "_defensive_mode", False) and conf < 0.70:
                logger.info("US 잔여 소진 차단 (defensive, %s): 확신도 %.0f%% < 70%%", name, conf*100)
                return
            # 재진입 차단 / 일일 매도 한도 검증 — 4/29 RIOT churn loop 핵심 fix
            blocked, br = self._is_reentry_blocked(ticker)
            if blocked:
                logger.info("US 잔여 소진 차단 (현재 종목 %s): %s", name, br)
                return
            exceeded, ex = self._is_daily_sell_limit(ticker)
            if exceeded:
                logger.info("US 잔여 소진 차단 (현재 종목 %s): %s", name, ex)
                return

            bal_final = self.client.get_us_balance()
            kis_cash = bal_final.get("cash_usd", 0.0)
            # T+1 결제 보정 — 이번 사이클에 이미 매수한 만큼 빼야 진짜 가용 자금
            pending_used = float(getattr(self, "_pending_us_buy_usd", 0.0))
            remaining_usd = max(kis_cash - pending_used, 0.0)
            if remaining_usd >= self.min_amount_usd:
                from zusik.analysis.indicators import slow_bleed as _sb, momentum_score as _ms
                mom_min = float((self.config.get("position", {}) or {}).get("leftover_momentum_min", 0.10))
                # 이 종목이 buy/hold + 현재 종목 모멘텀이 바를 넘을 때만 추가 매수.
                # 평평하면(미달) 아래 else 로 떨어져 '움직이는' 대체 종목을 찾고, 그것도 없으면 현금 유지.
                if (signal in ("buy", "long_term_buy", "hold")
                        and remaining_usd >= price > 0 and self._leftover_momentum_ok(df)):
                    add_qty = int(remaining_usd / (price * 1.005))
                    if add_qty >= 1:
                        logger.info("US 잔여 $%.2f → %s 추가 %d주 매수",
                                    remaining_usd, name, add_qty)
                        res = self.client.buy_us_limit(ticker, add_qty, price * 1.005, exchange)
                        if res.get("success"):
                            self._pending_us_buy_usd += add_qty * price * 1.005
                            fx_now = self.client.get_usd_krw_rate()
                            self.positions.record_buy(ticker, name, add_qty, price)
                            self.tracker.record_buy(ticker, name, add_qty,
                                                    int(price * fx_now), False, "US 잔여 소진")
                            if self.discord:
                                self.discord.notify_trade(
                                    side="buy", stock_name=f"{name} (US)", stock_code=ticker,
                                    qty=add_qty, price=int(price * 100),
                                    reason=f"잔여 ${remaining_usd:.2f} 소진",
                                )
                else:
                    # 다른 US 종목 중 기술적 양호 + 저가 종목으로 소진
                    # (bleed/급락/무모멘텀 종목 제외 — 방금 산 MARA 같은 케이스 방지)
                    for alt in self.us_stocks:
                        at = alt["ticker"]
                        if at == ticker:
                            continue
                        # 장전 게이트는 실제 매수 종목 기준으로 재평가 — 바깥 게이트는 원래
                        # 종목(symbol=ticker) 기준이라 whitelist 우회가 대체 종목으로 새면 안 됨
                        allow_alt, alt_pm = self._pre_market_buy_gate("US", conf, symbol=at)
                        if not allow_alt:
                            logger.debug("US 잔여 소진 후보 %s 제외: %s", at, alt_pm)
                            continue
                        # 재진입 차단된 종목은 잔여 소진 후보에서도 제외
                        alt_blocked, alt_br = self._is_reentry_blocked(at)
                        if alt_blocked:
                            logger.debug("US 잔여 소진 후보 %s 제외: %s", at, alt_br)
                            continue
                        alt_limited, _ = self._is_daily_sell_limit(at)
                        if alt_limited:
                            logger.debug("US 잔여 소진 후보 %s 제외: 일일 매도 한도 도달", at)
                            continue
                        aex = alt.get("exchange", "NASD")
                        try:
                            ap = self.client.get_us_current_price(at, aex)
                            a_price = ap["price"]
                            if a_price <= 0 or a_price > remaining_usd:
                                continue
                            # 기술 필터: df 조회 후 출혈/약세 제외
                            a_df = self.client.get_us_ohlcv(at, aex)
                            if a_df is None or a_df.empty:
                                continue
                            bleed = _sb(a_df)
                            mom = _ms(a_df)
                            recent_high = float(a_df["high"].iloc[-20:].max())
                            from_high = (a_price - recent_high) / recent_high if recent_high > 0 else 0
                            # 일중 변동성도 가드 — 매수 직후 crash_instant trigger 방지
                            try:
                                pc_info = self.client.get_us_current_price(at, aex)
                                prev_close = float(pc_info.get("prev_close", 0) or 0)
                                if prev_close > 0:
                                    intraday = (a_price - prev_close) / prev_close
                                    if intraday <= -0.04:
                                        logger.info("US 잔여 소진 후보 %s 제외: 일중 %+.1f%% (crash_instant 직전)",
                                                    at, intraday * 100)
                                        continue
                            except Exception:
                                pass
                            if bleed["is_bleeding"] or not self._leftover_momentum_ok(a_df) or from_high <= -0.15:
                                logger.info("US 잔여 소진 후보 %s 제외: 출혈=%s 모멘텀=%+.2f(<%.2f) 고점대비=%+.1f%%",
                                            at, bleed["is_bleeding"], mom, mom_min, from_high * 100)
                                continue
                            a_qty = int(remaining_usd / (a_price * 1.005))
                            if a_qty < 1:
                                continue
                            an = alt.get("name", at)
                            logger.info("US 잔여 $%.2f → %s %d주 소진 매수 (모멘텀 %+.2f)",
                                        remaining_usd, an, a_qty, mom)
                            res = self.client.buy_us_limit(at, a_qty, a_price * 1.005, aex)
                            if res.get("success"):
                                self._pending_us_buy_usd += a_qty * a_price * 1.005
                                fx_now = self.client.get_usd_krw_rate()
                                self.positions.record_buy(at, an, a_qty, a_price)
                                self.tracker.record_buy(at, an, a_qty,
                                                        int(a_price * fx_now), False, "US 잔여 소진")
                                if self.discord:
                                    self.discord.notify_trade(
                                        side="buy", stock_name=f"{an} (US)", stock_code=at,
                                        qty=a_qty, price=int(a_price * 100),
                                        reason=f"잔여 ${remaining_usd:.2f} 소진",
                                    )
                                break
                        except Exception:
                            continue
        except Exception as e:
            logger.warning("US 잔여 소진 체크 오류: %s", e)

    def _handle_us_buy(self, ticker: str, name: str, price: float, exchange: str, df=None,
                       is_long_term: bool = False, hedge_base_ratio: float | None = None,
                       fast_entry: bool = False):
        # 인버스 헷지는 현금예약 차단 우회 (예약 현금은 헷지용,
        if getattr(self, "_buy_blocked_low_cash", False) and hedge_base_ratio is None:
            logger.info("US 매수 차단 (현금 부족): %s", name)
            return
        pause_until = self.config.get("buy_paused_until")
        if pause_until:
            try:
                limit = datetime.fromisoformat(pause_until)
                if datetime.now() < limit:
                    logger.info("US 매수 차단 (일시 정지 %s까지): %s", pause_until, name)
                    return
            except Exception:
                pass
        # 개장 급변동 가드 (US): 개장 직후 N분 신규 매수 보류 (시초 갭/스파이크 추격 방지).
        # 헷지·추가매수(피라미딩)·빠른진입(fast_entry)은 제외 — 확인된 급등/반등 신호는 진입.
        if hedge_base_ratio is None and not fast_entry \
                and not self.positions.is_pyramid_eligible(ticker, price) \
                and self._in_opening_window("US"):
            logger.info("US 매수 보류 (개장 변동성 가드): %s — 시초 급변동 안정 후 진입", name)
            return
        # churning 방지 — 종목당 cooldown + 일 N회 한도
        #: 인버스 헷지(hedge_base_ratio)는 우회 — KR과 동일 (급락 중 분할 증액 허용,
        # 총 노출은 _inverse_under_max_ratio가 별도 cap).
        if hedge_base_ratio is None:
            try:
                pos_cfg = self.config.get("position", {})
                cooldown_min = int(pos_cfg.get("buy_cooldown_minutes", 0))
                daily_limit = int(pos_cfg.get("daily_buy_count_per_stock", 0))
                today_str = datetime.now().strftime("%Y-%m-%d")
                recent_buys = [
                    t for t in self.tracker._trades
                    if t.get("type") == "buy" and (t.get("ticker") == ticker or t.get("code") == ticker)
                    and t.get("date", "") == today_str
                ]
                if daily_limit > 0 and len(recent_buys) >= daily_limit:
                    logger.info("US 매수 차단 (%s): 일일 매수 한도 %d회 도달", name, daily_limit)
                    return
                if cooldown_min > 0 and recent_buys:
                    last_buy = max(recent_buys, key=lambda t: t.get("timestamp", ""))
                    try:
                        last_dt = datetime.fromisoformat(last_buy.get("timestamp", ""))
                        elapsed = (datetime.now() - last_dt).total_seconds() / 60
                        if elapsed < cooldown_min:
                            logger.info("US 매수 차단 (%s): cooldown %d분 중 (남은 %d분)",
                                        name, cooldown_min, int(cooldown_min - elapsed))
                            return
                    except Exception:
                        pass
            except Exception:
                pass
        if self._churn_guard(ticker, name, df=df, price=price,
                             is_add_on=self.positions.is_pyramid_eligible(ticker, price)):
            return

        _is_add_on = self.positions.is_pyramid_eligible(ticker, price)
        quality_ok, quality_reason = self._entry_quality_gate(
            df, is_inverse=self._is_inverse(ticker), is_add_on=_is_add_on)
        if not quality_ok:
            logger.info("US 진입 품질 차단 (%s): %s", name, quality_reason)
            return
        # 장전 리포트 sentiment gate (인버스는 우회)
        if not self._is_inverse(ticker):
            analysis = self.strategy.get_last_analysis() if self.use_claude else None
            conf = (analysis or {}).get("confidence", 0.5) if analysis else 0.5
            allow_pm, pm_reason = self._pre_market_buy_gate("US", conf, symbol=ticker)
            if not allow_pm:
                logger.info("US 매수 차단 (%s): %s", name, pm_reason)
                return
        if self._is_inverse(ticker):
            allow, reason = self._should_allow_inverse_entry()
            if not allow:
                logger.info("US 인버스 매수 차단 %s: %s", name, reason)
                return
            logger.info("US 인버스 매수 허용 %s: %s", name, reason)
        elif getattr(self, "_fast_fall_active", False):
            logger.info("급락 가드 — US 신규 매수 중단 (%s): %s", self._market_condition, name)
            return
        elif getattr(self, "_defensive_mode", False):
            analysis = self.strategy.get_last_analysis() or {}
            conf = analysis.get("confidence", 0) or 0
            if conf < 0.70:
                logger.info("US 보수 모드(%s): 확신도 %.0f%% < 70%% → 매수 보류 (%s)",
                            self._market_condition, conf * 100, name)
                return
        try:
            us_balance = self.client.get_us_balance()
        except Exception as e:
            logger.warning("US 잔고 조회 실패: %s", e)
            return
        cash_usd = us_balance["cash_usd"]
        if cash_usd < self.min_amount_usd:
            logger.info("US 매수 스킵 — 달러 잔고 $%.2f < 최소 $%.0f (환전 필요)", cash_usd, self.min_amount_usd)
            return

        # ── 피라미딩: 이미 보유 중 + 수익 구간이면 추가 배팅 (인버스 헷지는 자체 사이징 사용) ──
        holding = next((h for h in us_balance.get("holdings", []) if h["ticker"] == ticker), None)
        if holding and holding.get("qty", 0) > 0 and hedge_base_ratio is None:
            pyramid = self.positions.plan_add_on(ticker, price, cash_usd)
            if pyramid["qty"] > 0:
                add_qty = pyramid["qty"]
                logger.info("▶ US 피라미딩: %s %d주 × $%.2f (%s)",
                            name, add_qty, price, pyramid["reason"])
                res = self.client.buy_us_limit(ticker, add_qty, price * 1.005, exchange)
                if res.get("success"):
                    self._pending_us_buy_usd = float(getattr(self, "_pending_us_buy_usd", 0.0)) + add_qty * price * 1.005
                    fx_now = self.client.get_usd_krw_rate()
                    self.positions.record_buy(ticker, name, add_qty, price)
                    self._record_buy_with_context(
                        ticker, name, add_qty, int(price * fx_now), False,
                        f"[US 피라미딩 {pyramid['level']}차] {pyramid['reason']}",
                        entry_context=(self._last_entry_context(ticker)
                                       or self._build_reward_context(
                                           getattr(self, "_market_condition", "peace"),
                                           self._is_inverse(ticker))),
                        entry_indicators=self._last_entry_indicators(ticker),
                        entry_analyst_details=self._last_entry_analyst_details(ticker),
                    )
                    self.positions.record_pyramid(ticker, pyramid["level"])
                    if self.discord:
                        self.discord.notify_trade(
                            side="buy", stock_name=f"{name} (US)", stock_code=ticker,
                            qty=add_qty, price=int(price * 100),
                            reason=f"[피라미딩 {pyramid['level']}차] {pyramid['reason']}",
                        )
                return
        reason = ""
        long_term_reason = ""

        # 소액계좌(최소주문의 2배 이하): 올인. 경계 포함(≤)·min_amount 기준 — KR과 동일.
        # 정확히 $500(=min $50×2 기본은 아니나 하한 $500 유지) 계좌가 경계 탈락해 스로틀에
        # 막혀 매매 정지되던 버그(KR 20만 계좌와 같은 클래스). 인버스 헷지는 올인 금지.
        small_usd = cash_usd <= max(500, self.min_amount_usd * 2) and hedge_base_ratio is None
        conf = 0.5
        analysis = None
        if hedge_base_ratio is not None:
            base_ratio = hedge_base_ratio
            reason = "선제 헷지 (지수 급락)"
        elif self.use_claude:
            base_ratio = self.strategy.get_invest_ratio()
            analysis = self.strategy.get_last_analysis()
            reason = analysis.get("reasoning", "") if analysis else ""
            long_term_reason = analysis.get("long_term_reason", "") if analysis else ""
            conf = analysis.get("confidence", 0.5) if analysis else 0.5
        else:
            base_ratio = self.invest_ratio

        if small_usd:
            invest = cash_usd * max(base_ratio, 0.98)
        else:
            rvol = 0.0
            try:
                if df is not None and len(df) >= 10:
                    rvol = float(df["close"].pct_change().dropna().iloc[-20:].std())
            except Exception:
                rvol = 0.0
            adj_ratio, adj_reason = self._dynamic_invest_ratio(
                base_ratio, conf, is_inverse=self._is_inverse(ticker), symbol=ticker,
                realized_vol=rvol,
            )
            us_total = cash_usd + sum(
                float(h.get("eval_amount", 0) or 0)
                for h in us_balance.get("holdings", []))
            cash_ratio = (cash_usd / us_total) if us_total > 0 else 0.0
            adj_ratio, cash_reason = self._cash_deployment_ratio(
                adj_ratio, cash_ratio, conf, symbol=ticker)
            invest = cash_usd * adj_ratio
            if abs(adj_ratio - base_ratio) > 0.01:
                logger.info("US %s 투자비율 조정 %.3f → %.3f (%s)",
                            name, base_ratio, adj_ratio, adj_reason)
            if "초과현금" in cash_reason:
                logger.info("US %s 현금 집행 가속: %s", name, cash_reason)

        reward_context = self._build_reward_context(
            getattr(self, "_market_condition", "peace"),
            self._is_inverse(ticker),
        )
        multiplier = self.reward.get_invest_multiplier(self.strategy.name, ticker, context=reward_context)
        if multiplier != 1.0:
            invest *= multiplier

        consensus_mult, consensus_reason = self._consensus_invest_boost(analysis)
        if abs(consensus_mult - 1.0) > 0.01:
            invest *= consensus_mult
            logger.info("US %s 합의 배수 적용 ×%.2f (%s)", name, consensus_mult, consensus_reason)

        if getattr(self, "_daily_target_cooldown", False) and not self._is_inverse(ticker):
            if conf < self.daily_target_min_confidence:
                logger.info("US 일일 목표 쿨다운: 확신도 %.0f%% < %.0f%% → 신규 매수 보류 (%s)",
                            conf * 100, self.daily_target_min_confidence * 100, name)
                return
            invest *= self.daily_target_invest_ratio
            logger.info("US 일일 목표 쿨다운: %s 투자금 %.0f%% 축소", name,
                        self.daily_target_invest_ratio * 100)

        if is_long_term and not long_term_reason.strip():
            return

        # 핵심(whitelist) 종목 invest 하한 (reward/vol 디레이팅 이후) — NVDA 등 비싼 우량주가
        # 1주 미만으로 굶지 않게. KR과 동일 로직 (US는 USD 기준).
        _wl_floor_usd = self._whitelist_min_invest(ticker, cash_usd, cash_usd, price)
        if _wl_floor_usd > 0 and invest < _wl_floor_usd:
            logger.info("US %s 핵심주 invest 하한 적용: $%.2f → $%.2f", name, invest, _wl_floor_usd)
            invest = _wl_floor_usd

        if invest < self.min_amount_usd:
            # 중소 계좌 구제 (KR과 동일): 스로틀이 min 밑으로 눌러도 현금이 최소주문/1주를
            # 감당하면 최소주문만큼 태운다. 소액에서 '주문불가 → 매매정지' 변질 방지. 인버스 제외.
            if hedge_base_ratio is None and price > 0 and cash_usd >= min(self.min_amount_usd, price):
                floored = min(cash_usd, max(self.min_amount_usd, price))
                logger.info("US %s 소액 구제: 스로틀 $%.2f < 최소 $%.0f → $%.2f로 상향(현금 $%.2f)",
                            name, invest, self.min_amount_usd, floored, cash_usd)
                invest = floored
            else:
                return

        invest = min(invest, cash_usd)
        qty = int(invest / price) if price > 0 else 0
        if qty <= 0 and small_usd and price > 0 and cash_usd >= price * 1.005:
            # 95% 올인 밴드 갭 (KR과 동일): 주가가 (현금×0.95, 현금] 구간이면 0주 포기 → 1주 태움.
            # 1.005 = 지정가 버퍼(아래 buy_us_limit price*1.005)까지 현금이 감당해야 주문 통과.
            qty = 1
            logger.info("US %s 소액 1주 상향: invest $%.2f < 주가 $%.2f ≤ 현금 $%.2f",
                        name, invest, price, cash_usd)
        if qty <= 0:
            return

        tag = "[장기]" if is_long_term else "[단기]"
        logger.info("▶ US %s 매수: %s %d주 × $%.2f", tag, name, qty, price)

        result = self.client.buy_us_limit(ticker, qty, price * 1.005, exchange)
        if result.get("success"):
            self._pending_us_buy_usd = float(getattr(self, "_pending_us_buy_usd", 0.0)) + qty * price * 1.005
            fx_now = self.client.get_usd_krw_rate()
            price_krw = int(price * fx_now)
            self.positions.record_buy(ticker, name, qty, price)
            self._record_buy_with_context(
                ticker, name, qty, price_krw, is_long_term,
                long_term_reason if is_long_term else reason,
                entry_context=reward_context,
                entry_indicators=(analysis or {}).get("indicators", {}),
                entry_analyst_details=(analysis or {}).get("analyst_details", {}),
            )
            if self.discord:
                self.discord.notify_trade(
                    side="long_term_buy" if is_long_term else "buy",
                    stock_name=f"{name} (US)", stock_code=ticker, qty=qty, price=int(price * 100),
                    reason=reason, is_long_term=is_long_term, long_term_reason=long_term_reason,
                )

    def _handle_us_sell(self, ticker: str, name: str, exchange: str, df=None,
                        sell_ratio: float = 1.0):
        force_reason = getattr(self, "_us_force_sell_reason", None) or ""
        # 최소 보유 시간 체크 — 매수 후 10분 미만이면 매도 보류 (churn 방지)
        if not force_reason:
            last_buy = self.tracker.get_last_buy_time(ticker)
            if last_buy:
                elapsed = (datetime.now() - last_buy).total_seconds()
                if elapsed < 600:  # 10분
                    logger.info("%s — 매수 후 %d분 경과 (최소 10분), 매도 보류", name, int(elapsed / 60))
                    return

        us_balance = self.client.get_us_balance()
        holding = next((h for h in us_balance["holdings"] if h["ticker"] == ticker), None)
        if not holding or holding["qty"] <= 0:
            return

        total_qty = holding["qty"]
        price = holding["current_price"]
        avg_price = holding["avg_price"]
        profit_rate = holding.get("profit_rate", 0) / 100 if holding.get("profit_rate") else (
            (price - avg_price) / avg_price if avg_price > 0 else 0
        )
        reason = force_reason
        if not reason and self.use_claude:
            analysis = self.strategy.get_last_analysis()
            reason = analysis.get("reasoning", "") if analysis else ""

        # ── 분할 매도: 강제매도가 아니면 수익률 구간별로 일부 매도 ──
        if force_reason:
            #: sell_ratio 지원 — RSI 과매수 트림(0.5)이 전량 매도되던 문제 해결.
            # 손절/트레일링 등 기본(1.0)은 기존대로 전량.
            qty = max(1, int(total_qty * sell_ratio)) if sell_ratio < 1.0 else total_qty
            tranche_tag = ""
        else:
            if profit_rate >= 0.10:
                #: 강한 모멘텀이면 적게(25%) 덜고 라이딩 (폭등 수익 극대화).
                ride = (getattr(self.positions, "surge_ride_enabled", False) and df is not None
                        and self.positions._surge_momentum_intact(
                            df, getattr(self.positions, "surge_ride_rsi_max", 82.0)))
                ratio = getattr(self.positions, "surge_ride_trim_ratio", 0.25) if ride else 0.5
                qty = max(1, int(total_qty * ratio))
                tranche_tag = f"[익절 +10% {'라이딩' if ride else '50%'}]"
            elif profit_rate >= 0.05:
                qty = max(1, int(total_qty * 0.3))  # 5%+ 수익: 30% 익절
                tranche_tag = "[1차 익절 +5%]"
            elif profit_rate <= -0.05:
                qty = total_qty  # 손실 구간에서 SELL 신호면 전량 정리
                tranche_tag = "[손실 정리]"
            else:
                qty = max(1, int(total_qty * 0.5))  # 중립 구간: 절반만
                tranche_tag = "[중립 절반]"
        if tranche_tag:
            reason = f"{tranche_tag} {reason}".strip()

        logger.info("▶ US 매도: %s %d/%d주 @ $%.2f (수익률 %+.1f%%)",
                    name, qty, total_qty, price, profit_rate * 100)
        result = self.client.sell_us_limit(ticker, qty, price * 0.995, exchange)
        if result.get("success"):
            fx_now = self.client.get_usd_krw_rate()
            price_krw = int(price * fx_now)
            avg_krw = int(avg_price * fx_now)
            self.positions.record_sell(ticker, qty)
            pnl = self.tracker.record_sell(ticker, name, qty, price_krw, avg_krw, reason)
            self._record_sell_for_churn_guard(ticker, reason, sell_price=price)  # USD 기준 (재매수 비교도 USD)

            # 수동 진입은 자동 전략 학습에서 분리 (KR과 동일 — bot_kr._handle_sell 참조)
            _entry_reason = self.tracker.get_last_buy_reason(ticker)
            _strategy_bucket = "manual" if "수동" in _entry_reason else self.strategy.name
            self.reward.record_trade_result(
                stock_code=ticker, stock_name=name,
                strategy_name=_strategy_bucket,
                realized_pnl=pnl["realized_pnl"],
                realized_rate=pnl["realized_rate"],
                indicators=self._last_entry_indicators(ticker),
                context=(self._last_entry_context(ticker)
                         or self._build_reward_context(
                             getattr(self, "_market_condition", "peace"),
                             self._is_inverse(ticker))),
            )

            if self.discord:
                self.discord.notify_trade(
                    side="sell", stock_name=f"{name} (US)", stock_code=ticker,
                    qty=qty, price=int(price * 100), reason=reason,
                    realized_pnl=pnl["realized_pnl"], realized_rate=pnl["realized_rate"],
                )

            # 매도 후 다른 종목 즉시 탐색
            self._rotate_us_stock(ticker, proceeds=qty * price)

    def _rotate_us_stock(self, sold_ticker: str, proceeds: float):
        """매도 직후 — 다른 종목 중 매수할 만한 것 탐색."""
        others = [s for s in self.us_stocks if s["ticker"] != sold_ticker]
        if not others:
            return
        for stock in others:
            t = stock["ticker"]
            nm = stock.get("name", t)
            ex = stock.get("exchange", "NASD")
            try:
                p = self.client.get_us_current_price(t, ex)
                price = p["price"]
                if price <= 0 or price > proceeds:
                    continue
                df_us = self.client.get_us_ohlcv(t, ex)
                if df_us is None:
                    continue
                self.strategy.set_stock(t, nm)
                sig = self.strategy.analyze(df_us)
                if sig in ("buy", "long_term_buy"):
                    logger.info("매도→회전: %s %s → 매수 시도", nm, sig)
                    self._handle_us_buy(t, nm, price, ex, df_us, is_long_term=(sig == "long_term_buy"))
                    return
            except Exception:
                pass
        logger.info("매도→회전: 매수 대상 없음 (다음 사이클 재시도)")

    def _prepare_open_buys_us(self):
        """미국 장전(개장 30분 전): US 후보 OHLCV 예열 + 로컬 매수 우선순위 (하루 1회).

        KR `_prepare_open_buys`의 미국판. get_us_ohlcv/get_us_current_price 사용,
        ticker 기준 우선순위. Claude 미사용. 주문은 내지 않음(점수/예열만)."""
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_open_prep_date_us", "") == today:
            return
        self._open_prep_date_us = today
        try:
            from zusik.analysis.indicators import momentum_score
            scored: list[tuple[str, str, float]] = []
            for s in self.us_stocks:
                ticker = s.get("ticker")
                if not ticker or self._is_inverse(ticker):
                    continue
                exch = s.get("exchange", "NASD")
                try:
                    df = self.client.get_us_ohlcv(ticker, exchange=exch, period=self.period)
                except Exception:
                    continue
                if df is None or len(df) < 20:
                    continue
                try:
                    mom = float(momentum_score(df))
                except Exception:
                    mom = 0.0
                gap = 0.0
                try:
                    cur = float(self.client.get_us_current_price(ticker, exch).get("price", 0) or 0)
                    prev = float(df["close"].iloc[-1])
                    if cur > 0 and prev > 0:
                        gap = (cur - prev) / prev
                except Exception:
                    pass
                score = mom + max(-0.05, min(0.03, gap))
                scored.append((ticker, s.get("name", ticker), score))
            scored.sort(key=lambda x: x[2], reverse=True)
            self._open_priority_us = {t: i for i, (t, _n, _s) in enumerate(scored)}
            top = ", ".join(n for _t, n, _s in scored[:5])
            logger.info("US 장전 분석 완료: %d종 매수 우선순위 — 상위: %s", len(scored), top)
            if self.discord:
                try:
                    self.discord.notify_info(f"US 장전 분석 완료 — 개장 매수 우선순위 상위: {top}")
                except Exception:
                    pass
        except Exception:
            logger.debug("US 장전 매수 준비 실패", exc_info=True)

    def _on_us_market_open(self):
        """미국 개장 직후 1회: 준비된 우선순위로 즉시 매수 사이클 실행 (KR과 동일).

        US 정규장(22:30~05:00 KST)은 자정을 넘겨 KST 날짜가 2개에 걸친다. 가드를 KST 날짜로
        잡으면 00:00 에 날짜가 바뀌며 같은 세션에서 개장 킥이 한 번 더 발동(미장분석 2회) →
        세션 날짜로 가드: 정오(12시) 이후면 당일, 이전(자정~오전)이면 전일에 귀속해
        한 세션 = 한 번만 발동. DST(개장 22:30/23:30)·동절기(마감 06:00) 모두 cutoff 12시 안.
        """
        now = datetime.now()
        session_date = (now if now.hour >= 12 else now - timedelta(days=1)).strftime("%Y-%m-%d")
        if getattr(self, "_us_open_kicked_date", "") == session_date:
            return
        self._us_open_kicked_date = session_date
        logger.info("US 개장 — 준비된 우선순위로 즉시 매수 사이클 실행")
        import threading

        def _kick():
            try:
                self.run_us()
            except Exception:
                logger.debug("US 개장 즉시 매수 사이클 실패", exc_info=True)
        threading.Thread(target=_kick, daemon=True).start()

    def run_us(self):
        """미국 주식 매매 (미국 장중에만)."""
        if not getattr(self, "us_enabled", True):
            return   # config us_enabled=false — 미국 매매 전면 비활성
        if not self.client.is_us_market_open():
            return
        # 동시 실행 가드 — 개장 즉시 사이클 + 스케줄 run_once 겹침 방지 (KR과 동일)
        if getattr(self, "_us_running", False):
            logger.debug("run_us 이미 실행 중 — 중복 사이클 스킵")
            return
        self._us_running = True
        try:
            return self._run_us_inner()
        finally:
            self._us_running = False

    def _run_us_inner(self):
        if not self._check_risks_before_trading("US"):
            return
        if not self.us_stocks:
            logger.warning("US 신규 진입 후보 없음 — 보유 종목 청산 관리만 계속")

        logger.info("===== US 매매 [%s] =====", datetime.now().strftime("%H:%M:%S"))

        # 사이클 내 누적 매수액 추적 (T+1 결제 전엔 KIS cash_usd가 안 줄어드는 문제 보정).
        # 같은 사이클에서 여러 종목 분산 매수를 자기 자금으로 하려면, 봇이 직전 매수액을
        # 기억해서 다음 매수 가능 cash 계산에서 빼야 함.
        self._pending_us_buy_usd = 0.0

        # 이번 사이클 공유 잔고 스냅샷 — 강제매수 블록과 scan 병합 블록이 동일 값 재사용
        try:
            us_bal = self.client.get_us_balance()
            # 유령 포지션 정리(US) — positions.json 이 실잔고와 안 맞으면 churn 유발(KR 256750과 동일).
            # 잔고 조회 성공 시에만(아래 except 폴백 us_bal 은 빈 holdings 라 오청산 위험 → try 안에서).
            try:
                rem = self.positions.reconcile_holdings(
                    {h.get("ticker") for h in us_bal.get("holdings", [])}, market="US")
                if rem:
                    logger.warning("유령 포지션 정리(US, 실보유 0): %s", rem)
            except Exception:
                logger.debug("US 포지션 재조정 실패", exc_info=True)
        except Exception:
            logger.debug("US 잔고 스냅샷 실패", exc_info=True)
            us_bal = {"cash_usd": 0.0, "holdings": []}

        # 보유 0 + 달러 있으면 강제 매수
        force_bought_tickers = set()
        try:
            # 매수가능금액 별도 조회 (잔고 API가 외화예수금을 안 보여주는 버그 우회)
            allow_forced = bool((self.config.get("cash_deployment", {}) or {}).get(
                "allow_forced_orders", True))
            buying_power = 0.0
            if allow_forced:
                import requests
                self.client._ensure_token()
                headers = {
                    'authorization': f'Bearer {self.client._access_token}',
                    'appkey': self.client.app_key, 'appsecret': self.client.app_secret,
                    'content-type': 'application/json', 'tr_id': 'TTTS3007R',
                }
                params = {'CANO': self.client.account_no, 'ACNT_PRDT_CD': '01',
                          'OVRS_EXCG_CD': 'NASD', 'OVRS_ORD_UNPR': '0', 'ITEM_CD': 'SOFI'}
                r = requests.get(
                    f'{self.client.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount',
                    headers=headers, params=params, timeout=10)
                buying_power = float(r.json().get('output', {}).get('ord_psbl_frcr_amt', 0))

            if not us_bal.get("holdings") and buying_power > self.min_amount_usd:
                logger.info("US 보유 0 + $%.2f 매수가능 → Claude 판단 후 매수", buying_power)
                # Claude가 확신도 높은 순으로 매수
                us_candidates = []
                for stock in self.us_stocks:
                    ticker = stock["ticker"]
                    name = stock.get("name", ticker)
                    exchange = stock.get("exchange", "NASD")
                    try:
                        p = self.client.get_us_current_price(ticker, exchange)
                        price = p["price"]
                        df_us = self.client.get_us_ohlcv(ticker, exchange)
                        if df_us is None or price <= 0:
                            continue
                        self.strategy.set_stock(ticker, name)
                        sig = self.strategy.analyze(df_us)
                        analysis = self.strategy.get_last_analysis()
                        conf = analysis.get("confidence", 0) if analysis else 0
                        us_candidates.append({"ticker": ticker, "name": name, "exchange": exchange,
                                              "price": price, "signal": sig, "confidence": conf})
                    except Exception:
                        pass

                # buy/hold 우선, 없으면 sell 중 확신도 가장 낮은 것 (= 가장 덜 약세)
                buy_candidates = [c for c in us_candidates if c["signal"] != "sell"]
                # 약신호 강제매수 차단: 확신도 50% 미만은 제외
                # 4-20 NIO/GRAB이 확신도 42%로 강제매수 → 5일 후 slow_bleed로 -2,240원 손실
                # 강제매수는 보유 0 회피 목적이므로 최소한의 신호 강도가 필요
                MIN_FORCE_BUY_CONFIDENCE = 0.50
                buy_candidates = [c for c in buy_candidates
                                  if c.get("confidence", 0) >= MIN_FORCE_BUY_CONFIDENCE]
                if buy_candidates:
                    buy_candidates.sort(key=lambda x: x["confidence"], reverse=True)
                    us_candidates = buy_candidates
                else:
                    # 전부 SELL → 신호 존중, 현금 대기. ("3시간 유휴 시 최약 SELL 소량 진입"
                    # 분기는 리스트를 비운 뒤 빈 리스트를 정렬하던 죽은 코드라 삭제 —
                    # 실행 이력 0이었고, 전 종목 매도신호인 날의 진입은 신호 존중과 모순)
                    us_candidates = []
                    logger.info("US 전 종목 SELL → 현금 대기 (%d시간 유휴)", self._cash_idle_hours())
                logger.info("US 후보: %s", [(c["name"], c["signal"], f"{c['confidence']:.0%}") for c in us_candidates])
                remaining_usd = buying_power
                for uc in us_candidates:
                    # 강제매수도 신규 매수 — 장전 리스크오프(avoid_new_buy 등)·재진입 차단을
                    # 존중한다. "보유 0 회피"가 방어적 현금 결정을 뒤집으면 안 됨.
                    allow_pm, pm_reason = self._pre_market_buy_gate(
                        "US", uc.get("confidence", 0), symbol=uc["ticker"])
                    if not allow_pm:
                        logger.info("US 강제매수 차단 (%s): %s", uc["name"], pm_reason)
                        continue
                    fb_blocked, fb_br = self._is_reentry_blocked(uc["ticker"], uc["price"])
                    if fb_blocked:
                        logger.info("US 강제매수 차단 (%s): %s", uc["name"], fb_br)
                        continue
                    # 보수/쿨다운 국면엔 유휴 현금 해소보다 현금 유지 — 일반 매수 경로와 동일 기준
                    fb_conf = uc.get("confidence", 0)
                    if getattr(self, "_defensive_mode", False) and fb_conf < 0.70:
                        logger.info("US 강제매수 보류 (%s): defensive 확신도 %.0f%% < 70%%",
                                    uc["name"], fb_conf * 100)
                        continue
                    if getattr(self, "_daily_target_cooldown", False) and fb_conf < self.daily_target_min_confidence:
                        logger.info("US 강제매수 보류 (%s): 일일 목표 쿨다운 확신도 %.0f%% < %.0f%%",
                                    uc["name"], fb_conf * 100, self.daily_target_min_confidence * 100)
                        continue
                    qty = int(remaining_usd / uc["price"])
                    if qty >= 1:
                        result = self.client.buy_us_limit(uc["ticker"], qty, uc["price"] * 1.005, uc["exchange"])
                        if result.get("success"):
                            self._pending_us_buy_usd = float(getattr(self, "_pending_us_buy_usd", 0.0)) + qty * uc["price"] * 1.005
                            remaining_usd -= qty * uc["price"]
                            force_bought_tickers.add(uc["ticker"])
                            fx_now = self.client.get_usd_krw_rate()
                            # 포지션 상태 기록 — 없으면 이후 본전보호/트레일링/피라미딩이 이 보유를 모름
                            self.positions.record_buy(uc["ticker"], uc["name"], qty, uc["price"])
                            self.tracker.record_buy(uc["ticker"], uc["name"], qty, int(uc["price"] * fx_now), False,
                                                    f"US 강제매수 확신도 {uc['confidence']:.0%}")
                            from zusik.clients.discord_bot import send_trade_alert
                            send_trade_alert("buy", uc["name"], uc["ticker"], qty,
                                             f"${uc['price']:.2f}", f"US 강제매수 (확신도 {uc['confidence']:.0%})")
                            logger.info("US 강제매수: %s %d주 @ $%.2f", uc["name"], qty, uc["price"])
        except Exception:
            logger.warning("US 강제매수 체크 실패", exc_info=True)

        # 보유 종목은 스크리너 선별 결과와 무관하게 항상 매도 분석 대상에 포함
        scan_us = list(self.us_stocks)
        seen = {s.get("ticker", "") for s in scan_us}
        for h in us_bal.get("holdings", []):
            tk = h.get("ticker")
            if tk and tk not in seen:
                scan_us.append({
                    "ticker": tk,
                    "name": h.get("name", tk),
                    "exchange": h.get("exchange", "NASD"),
                })
                if tk not in self._merge_logged_us:
                    logger.info("US 보유 종목 분석 대상 추가: %s(%s)", h.get("name", tk), tk)
                    self._merge_logged_us.add(tk)
                seen.add(tk)

        # 인버스 US ETF 상시 편입 (A)
        if self.config.get("inverse", {}).get("enabled", True):
            for inv in self._inverse_us_list():
                if inv["ticker"] not in seen:
                    scan_us.append(inv)
                    seen.add(inv["ticker"])

        held_us_tickers = {h.get("ticker") for h in us_bal.get("holdings", [])}
        allow_inverse_us, inverse_reason_us = self._should_allow_inverse_entry()

        # 달러 예수금이 최소매수 미만이면 미보유 US 종목은 분석 자체 스킵 → API 절약
        us_cash_now = float(us_bal.get("cash_usd", 0) or 0)
        if us_cash_now < self.min_amount_usd:
            before = len(scan_us)
            scan_us = [s for s in scan_us if s.get("ticker") in held_us_tickers]
            skipped = before - len(scan_us)
            if skipped > 0:
                logger.info("US 달러 잔고 $%.2f < 최소매수 $%.0f → 미보유 %d종 분석 스킵 (API 절약)",
                            us_cash_now, self.min_amount_usd, skipped)

        # US 분석 동시화
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _exec_safe_us(stock):
            tk = stock.get("ticker", "")
            try:
                self._execute_us_stock(stock)
            except Exception as e:
                logger.exception("%s 오류", tk)
                msg = self._format_error_alert("US", stock.get("name", tk), e)
                if self.discord and msg:
                    self.discord.notify_error(msg)

        candidates_us = []
        for stock in scan_us:
            tk = stock.get("ticker", "")
            if tk in force_bought_tickers:
                logger.info("US %s — 이번 사이클 강제매수 직후, 분석 스킵", stock.get("name", tk))
                continue
            if self._is_inverse(tk) and tk not in held_us_tickers and not allow_inverse_us:
                logger.debug("인버스 %s 분석 스킵: %s", stock.get("name", tk), inverse_reason_us)
                continue
            candidates_us.append(stock)

        # 장전 산출 우선순위가 오늘자면 그 순서로 정렬 → 개장 시 좋은 후보부터 매수 (KR과 동일)
        today = datetime.now().strftime("%Y-%m-%d")
        prio_us = getattr(self, "_open_priority_us", None)
        if prio_us and getattr(self, "_open_prep_date_us", "") == today:
            candidates_us.sort(key=lambda s: prio_us.get(s.get("ticker", ""), 10_000))
            logger.info("US 장전 우선순위 적용: 상위 %s",
                        ", ".join(s.get("name", s.get("ticker", "")) for s in candidates_us[:5]))
        #: 보유 인버스 최우선 슬롯 (KR과 동일 — 청산 지연 방지)
        candidates_us.sort(key=lambda s: 0 if (s.get("ticker", "") in held_us_tickers
                                               and self._is_inverse(s.get("ticker", ""))) else 1)
        max_workers = self.config.get("analysis_max_workers", 4)
        logger.info("US 분석 병렬: %d종목 / %d workers", len(candidates_us), max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_exec_safe_us, s): s for s in candidates_us}
            for f in as_completed(futures):
                try:
                    f.result(timeout=180)
                except Exception:
                    pass
