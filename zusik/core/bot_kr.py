from __future__ import annotations

import logging
import time
from datetime import datetime


logger = logging.getLogger(__name__)


class KRTradingMixin:
    """KRTradingMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _get_unsettled_kr_cash(self) -> int:
        """T+2 미정산 매도대금 — 최근 2영업일 내 KR 매도 금액 합계."""
        try:
            now = datetime.now()
            unsettled = 0
            for t in self.tracker.get_trades_today():
                pass  # 오늘 매도는 아래에서 처리

            # 최근 3일 매도 기록 확인 (주말 고려해서 3일)
            trades = self.tracker._trades
            for t in trades:
                if t.get("type") != "sell":
                    continue
                code = t.get("code", "")
                # US 종목은 T+2 해당 없음 (달러 별도)
                if not code.isdigit():
                    continue
                try:
                    sell_time = datetime.fromisoformat(t["timestamp"])
                    days_ago = (now - sell_time).days
                    # T+2: 매도 당일(D+0)과 D+1은 미정산
                    if days_ago < 2:
                        unsettled += t.get("amount", 0)
                except Exception:
                    pass
            return unsettled
        except Exception:
            return 0

    def _handle_buy(self, code: str, name: str, price: int, df=None, is_long_term: bool = False,
                    mtf: dict | None = None, hedge_base_ratio: float | None = None,
                    fast_entry: bool = False):
        # hedge_base_ratio: 인버스 선제 헷지 전용. 설정 시 직전 종목의 stale 분석을 쓰지 않고
        # 이 값을 base_ratio로 깨끗하게 사이징한다 (_handle_inverse에서만 호출).
        # 인버스 헷지는 현금예약 차단을 우회 — 예약 현금은 애초에 헷지를 위한 것.
        if getattr(self, "_buy_blocked_low_cash", False) and hedge_base_ratio is None:
            logger.info("매수 차단 (현금 부족): %s", name)
            return
        # config.buy_paused_until: 사용자 명시 일시 정지 (시간 가드)
        pause_until = self.config.get("buy_paused_until")
        if pause_until:
            try:
                limit = datetime.fromisoformat(pause_until)
                if datetime.now() < limit:
                    logger.info("매수 차단 (일시 정지 %s까지): %s", pause_until, name)
                    return
            except Exception:
                pass
        # 개장 급변동 가드: 개장 직후 N분간 신규 매수 보류 (시초 갭/스파이크 추격 방지).
        # 헷지·추가매수(피라미딩)·빠른진입(fast_entry: 확인된 급등돌파/과매도반등)은 제외 —
        # 단순 보류가 아니라 '오를 신호'가 확인된 종목은 개장에도 진입한다.
        if hedge_base_ratio is None and not fast_entry \
                and not self.positions.is_pyramid_eligible(code, price) \
                and self._in_opening_window("KR"):
            logger.info("매수 보류 (개장 변동성 가드, KR): %s — 시초 급변동 안정 후 진입", name)
            return
        if self._churn_guard(code, name, df=df, price=price,
                             is_add_on=self.positions.is_pyramid_eligible(code, price)):
            return

        # 신규 진입 품질: 약한 모멘텀과 과열 추격은 LLM BUY라도 차단한다.
        _is_add_on = self.positions.is_pyramid_eligible(code, price)
        quality_ok, quality_reason = self._entry_quality_gate(
            df, is_inverse=self._is_inverse(code), is_add_on=_is_add_on)
        if not quality_ok:
            logger.info("진입 품질 차단 (%s): %s", name, quality_reason)
            return
        # churning 방지 — 같은 종목 매수 후 4시간 cooldown + 일 N회 한도
        #: 인버스 헷지(hedge_base_ratio)는 우회 — 급락 중 분할 증액(1·2·3차)을
        # 막으면 안 됨. 총 노출은 _inverse_under_max_ratio(20%)가 별도로 cap.
        #(폭락일 실증 1차 체결 후 4h cooldown에 막혀 헷지가 전혀 증액되지 못함)
        if hedge_base_ratio is None:
            try:
                pos_cfg = self.config.get("position", {})
                cooldown_min = int(pos_cfg.get("buy_cooldown_minutes", 0))
                daily_limit = int(pos_cfg.get("daily_buy_count_per_stock", 0))
                today_str = datetime.now().strftime("%Y-%m-%d")
                recent_buys = [
                    t for t in self.tracker._trades
                    if t.get("type") == "buy" and t.get("code") == code
                    and t.get("date", "") == today_str
                ]
                if daily_limit > 0 and len(recent_buys) >= daily_limit:
                    logger.info("매수 차단 (%s): 일일 매수 한도 %d회 도달", name, daily_limit)
                    return
                if cooldown_min > 0 and recent_buys:
                    last_buy = max(recent_buys, key=lambda t: t.get("timestamp", ""))
                    try:
                        last_dt = datetime.fromisoformat(last_buy.get("timestamp", ""))
                        elapsed = (datetime.now() - last_dt).total_seconds() / 60
                        if elapsed < cooldown_min:
                            logger.info("매수 차단 (%s): cooldown %d분 중 (남은 %d분)",
                                        name, cooldown_min, int(cooldown_min - elapsed))
                            return
                    except Exception:
                        pass
            except Exception:
                pass

        # 매수 차단 — RSI > 78 + 최근 5일 +10%+ 상승 종목 매수 X
        # (어제 LG이노텍/SK네트웍스 추격 매수 → 다음날 -6/-11% 손절 패턴 반복 방지)
        # 인버스는 우회 — '오를 때 사야 하는' 헷지라 RSI/급등 추격 차단이 모순.
        if df is not None and not self._is_whitelist(code) and not self._is_inverse(code):
            try:
                from zusik.analysis.indicators import calc_rsi as _rsi
                rsi_val = _rsi(df).iloc[-1] if len(df) > 14 else 50
                close = df["close"]
                surge_5d = (close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0
                if (rsi_val > 78) and (surge_5d > 0.10):
                    logger.info("추격 매수 차단 (%s): RSI %.0f + 5일 %+.1f%% (이미 과열)",
                                name, rsi_val, surge_5d * 100)
                    return
            except Exception:
                pass
        # 장전 리포트 sentiment gate (인버스는 우회 — 별도 bear score 기반)
        if not self._is_inverse(code):
            analysis = self.strategy.get_last_analysis() if self.use_claude else None
            conf = (analysis or {}).get("confidence", 0.5) if analysis else 0.5
            allow_pm, pm_reason = self._pre_market_buy_gate("KR", conf, symbol=code)
            if not allow_pm:
                logger.info("KR 매수 차단 (%s): %s", name, pm_reason)
                return
        if self._is_inverse(code):
            allow, reason = self._should_allow_inverse_entry()
            if not allow:
                logger.info("인버스 매수 차단 %s: %s", name, reason)
                return
            logger.info("인버스 매수 허용 %s: %s", name, reason)
        elif getattr(self, "_fast_fall_active", False):
            logger.info("급락 가드 — 신규 매수 중단 (%s): %s", self._market_condition, name)
            return
        elif getattr(self, "_defensive_mode", False):
            analysis = self.strategy.get_last_analysis() or {}
            conf = analysis.get("confidence", 0) or 0
            if conf < 0.70:
                logger.info("보수 모드(%s): 확신도 %.0f%% < 70%% → 매수 보류 (%s)",
                            self._market_condition, conf * 100, name)
                return
        balance = self.client.get_balance()
        # KIS의 'cash'(orderable_cash)는 매도 즉시 재사용 가능분(sll_ruse)을 이미 반영.
        # _get_unsettled_kr_cash 차감하면 중복 → 매도 후 후속 매수 차단되는 버그.
        cash = balance["cash"]

        reason = ""
        long_term_reason = ""
        news_text = ""

        # 소액계좌(최소주문의 2배 이하): 현금 95% 전액 투입 (분할 의미 없음).
        # 경계 포함(≤): 정확히 20만(=min_amount 10만×2) 계좌가 경계에서 탈락해 동적 스로틀에
        # 막혀 매매 전면 정지되던 버그(사용자 2026-07-01). min_amount 기준이라 설정 변경에 자동 대응.
        # 인버스 헷지는 소액 올인 금지 — 테일헷지는 항상 분할 (max_ratio 한도 내).
        small_account = cash <= max(200_000, self.min_amount * 2) and hedge_base_ratio is None

        conf = 0.5
        analysis = None
        if hedge_base_ratio is not None:
            # 인버스 선제 헷지 — stale 분석 미사용, 깨끗한 컨텍스트로 사이징
            base_ratio = hedge_base_ratio
            reason = "선제 헷지 (지수 급락)"
        elif self.use_claude:
            base_ratio = self.strategy.get_invest_ratio()
            analysis = self.strategy.get_last_analysis()
            reason = analysis.get("reasoning", "") if analysis else ""
            long_term_reason = analysis.get("long_term_reason", "") if analysis else ""
            news_text = analysis.get("news_summary", "") if analysis else ""
            conf = analysis.get("confidence", 0.5) if analysis else 0.5
        elif self.use_adaptive and df is not None:
            base_ratio = self.strategy.calc_position_ratio(df)
        else:
            base_ratio = self.invest_ratio

        # 총자산 — 양 분기에서 참조(whitelist 하한 등)하므로 if 앞에서 정의(소액 분기 미정의 방지).
        try:
            total_asset = int(balance.get("cash", 0)) + int(balance.get("total_eval", 0))
        except Exception:
            total_asset = int(cash)

        if small_account:
            invest = int(cash * max(base_ratio, 0.95))
        else:
            rvol = 0.0
            try:
                if df is not None and len(df) >= 10:
                    rvol = float(df["close"].pct_change().dropna().iloc[-20:].std())
            except Exception:
                rvol = 0.0
            adj_ratio, adj_reason = self._dynamic_invest_ratio(
                base_ratio, conf, is_inverse=self._is_inverse(code), symbol=code,
                realized_vol=rvol,
            )
            cash_ratio = (float(cash) / float(total_asset)) if total_asset > 0 else 0.0
            adj_ratio, cash_reason = self._cash_deployment_ratio(
                adj_ratio, cash_ratio, conf, symbol=code)
            # cap을 자산 기준으로 환산: invest = total_asset × ratio (단 cash 한도 내)
            invest = int(total_asset * adj_ratio)
            invest = min(invest, int(cash))  # 가용 현금 한도
            if abs(adj_ratio - base_ratio) > 0.01:
                logger.info("%s 투자비율 조정 %.3f → %.3f (%s)",
                            name, base_ratio, adj_ratio, adj_reason)
            if "초과현금" in cash_reason:
                logger.info("%s 현금 집행 가속: %s", name, cash_reason)

        # ── 실적 캘린더 체크 ──
        if news_text:
            earnings = self.positions.check_earnings_blackout(code, name, news_text)
            if earnings["in_blackout"]:
                logger.info("실적 블랙아웃: %s", earnings["reason"])
                return

        # ── 상관관계 필터 (소액계좌 스킵, 같은 종목 추가매수 제외, whitelist/인버스 우회) ──
        held_codes = [h["code"] for h in balance.get("holdings", []) if h.get("qty", 0) > 0 and h["code"] != code]
        if held_codes and df is not None and not small_account and not self._is_whitelist(code) and not self._is_inverse(code):
            ohlcv_cache = {code: df}
            for hc in held_codes:
                try:
                    hdf = self.client.get_ohlcv(hc, period=self.period)
                    if hdf is not None:
                        ohlcv_cache[hc] = hdf
                except Exception:
                    pass
            adapt_corr = self._adaptive_params().get("correlation")
            corr = self.positions.check_correlation(code, held_codes, ohlcv_cache,
                                                    threshold=adapt_corr)
            if not corr["allowed"]:
                logger.info("상관관계 필터: %s", corr["reason"])
                return

        # 보상 가중치
        reward_context = self._build_reward_context(
            getattr(self, "_market_condition", "peace"),
            self._is_inverse(code),
            breakout_bias=bool(mtf and mtf.get("aligned")),
        )
        multiplier = self.reward.get_invest_multiplier(self.strategy.name, code, context=reward_context)
        if multiplier != 1.0:
            invest = int(invest * multiplier)

        consensus_mult, consensus_reason = self._consensus_invest_boost(analysis)
        if abs(consensus_mult - 1.0) > 0.01:
            invest = int(invest * consensus_mult)
            logger.info("%s 합의 배수 적용 ×%.2f (%s)", name, consensus_mult, consensus_reason)

        if getattr(self, "_daily_target_cooldown", False) and not self._is_inverse(code):
            if conf < self.daily_target_min_confidence:
                logger.info("일일 목표 쿨다운: 확신도 %.0f%% < %.0f%% → 신규 매수 보류 (%s)",
                            conf * 100, self.daily_target_min_confidence * 100, name)
                return
            invest = int(invest * self.daily_target_invest_ratio)
            logger.info("일일 목표 쿨다운: %s 투자금 %.0f%% 축소", name,
                        self.daily_target_invest_ratio * 100)

        # 멀티 타임프레임 부스트
        if mtf and mtf.get("aligned"):
            invest = int(invest * 1.15)
            logger.info("멀티TF 정렬 보너스 +15%%")

        # 핵심(whitelist) 종목 invest 하한 — reward/변동성 디레이팅 이후에 적용해야
        # 우량주가 1주 미만으로 굶지 않음 (삼성/하이닉스 미매수 근본 원인 수정).
        _wl_floor = self._whitelist_min_invest(code, total_asset, cash, price)
        if _wl_floor > 0 and invest < _wl_floor:
            logger.info("%s 핵심주 invest 하한 적용: %s → %s원 (reward/vol 디레이팅 무시)",
                        name, f"{int(invest):,}", f"{int(_wl_floor):,}")
            invest = int(_wl_floor)

        if is_long_term:
            if not long_term_reason.strip():
                logger.warning("장기투자 사유 없음 → 취소")
                return
            if not self._check_long_term_limit(invest):
                return

        if invest < self.min_amount:
            # 중소 계좌 구제: 동적 스로틀이 invest 를 min_amount 밑으로 눌러도, 현금이 최소주문
            # (또는 1주)을 감당하면 그만큼은 태운다. 스로틀은 대형계좌 리스크 제어인데 소액에선
            # '주문 불가 → 매매 전면 정지'로 변질(20만~수십만 계좌가 14% 스로틀에 막힘).
            # 인버스 헷지는 제외(테일헷지 분할 규율 유지 — max_ratio 캡이 담당).
            if hedge_base_ratio is None and price > 0 and cash >= min(self.min_amount, price):
                floored = min(int(cash), max(self.min_amount, price))
                logger.info("%s 소액 구제: 스로틀 결과 %s < 최소주문 %s → %s로 상향(현금 %s)",
                            name, f"{int(invest):,}", f"{self.min_amount:,}",
                            f"{floored:,}", f"{int(cash):,}")
                invest = floored
            else:
                return
        invest = min(invest, cash)

        # ── 분할 매수 (소액은 전량 1회) ──
        # ── 피라미딩: 이미 보유 중 + 수익 구간이면 분할매수 대신 추가 배팅 ──
        if self.positions.has_position(code):
            pyramid = self.positions.plan_add_on(code, price, cash)
            if pyramid["qty"] > 0:
                qty = pyramid["qty"]
                tranche = 0  # 피라미딩은 tranche 체계 밖
                remaining = 0
                reason = f"[피라미딩 {pyramid['level']}차] {pyramid['reason']}"
                logger.info("▶ 피라미딩 매수: %s %d주 × %s원 (%s)",
                            name, qty, f"{price:,}", pyramid["reason"])
                if not self.order_guard.can_order(code, "buy"):
                    return
                result = None
                for attempt in range(3):
                    try:
                        result = self.client.buy_market(code, qty)
                        self.network.record_success()
                        break
                    except Exception as e:
                        self.network.record_failure()
                        logger.warning("피라미딩 실패 (%d/3): %s", attempt + 1, e)
                        if attempt < 2:
                            time.sleep(1 * (attempt + 1))
                if result and result.get("success"):
                    self._record_buy_with_context(
                        code, name, qty, price, False, reason,
                        entry_context=(self._last_entry_context(code)
                                       or self._build_reward_context(
                                           getattr(self, "_market_condition", "peace"),
                                           self._is_inverse(code))),
                        entry_indicators=self._last_entry_indicators(code),
                        entry_analyst_details=self._last_entry_analyst_details(code),
                    )
                    self.positions.record_buy(code, name, qty, price)
                    self.positions.record_pyramid(code, pyramid["level"])
                    if self.discord:
                        self.discord.notify_trade(
                            side="buy", stock_name=name, stock_code=code,
                            qty=qty, price=price, reason=reason,
                        )
                return

        if small_account:
            qty = invest // price if price > 0 else 0
            if qty <= 0 and price > 0 and cash >= price:
                # 95% 올인 밴드 갭: 주가가 (현금×0.95, 현금] 구간이면 invest//price=0으로
                # 조용히 포기 → 후보가 적은 완전 소액 계좌에선 매매 정지와 동일. 1주는 태운다.
                qty = 1
                logger.info("%s 소액 1주 상향: invest %s < 주가 %s ≤ 현금 %s",
                            name, f"{int(invest):,}", f"{price:,}", f"{int(cash):,}")
            if qty <= 0:
                return
            tranche = 1
            remaining = 0
        else:
            #: 인버스 헷지는 별도 tranches 적용. bear 0.50/0.65/0.80 구간별
            # 0.35/0.30/0.35 분할 진입. position.buy_tranches=[1.0]이 헷지 분할 증액을
            # 차단했던 폭락일 버그(헷지 1회 후 본격 급락 시 추가 매수 0) 근본 해결.
            inverse_tranches = None
            skip_dip = False
            if hedge_base_ratio is not None:
                pos_cfg = self.config.get("position", {})
                inverse_tranches = pos_cfg.get("inverse_buy_tranches", [0.35, 0.30, 0.35])
                skip_dip = True
            buy_plan = self.positions.plan_buy(
                code, invest, price,
                tranches_override=inverse_tranches,
                skip_dip_check=skip_dip,
            )
            if buy_plan.get("skip_reason"):
                logger.info("분할매수: %s", buy_plan["skip_reason"])
                return
            if buy_plan["qty"] <= 0:
                return
            qty = buy_plan["qty"]
            tranche = buy_plan["tranche"]
            remaining = buy_plan["remaining_tranches"]

        tag = "[장기]" if is_long_term else "[단기]"
        logger.info("▶ %s %d차 매수: %s %d주 × %s원 (남은 차수: %d)",
                     tag, tranche, name, qty, f"{price:,}", remaining)
        if is_long_term:
            logger.info("  사유: %s", long_term_reason)

        # 주문 가드: 중복 방지
        if not self.order_guard.can_order(code, "buy"):
            return

        # API 재시도 (최대 2회)
        result = None
        for attempt in range(3):
            try:
                result = self.client.buy_market(code, qty)
                self.network.record_success()
                break
            except Exception as e:
                self.network.record_failure()
                logger.warning("매수 API 실패 (%d/3): %s — %s", attempt + 1, code, e)
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))

        if not result or not result.get("success"):
            logger.error("매수 최종 실패: %s %d주 — %s", name, qty, result.get("message", "") if result else "응답 없음")
            return

        self.order_guard.record_order(code, "buy", qty, price, result.get("order_no", ""))
        if result.get("success"):
            self.positions.record_buy(code, name, qty, price)
            self._record_buy_with_context(
                code, name, qty, price, is_long_term,
                long_term_reason if is_long_term else reason,
                entry_context=reward_context,
                entry_indicators=(analysis or {}).get("indicators", {}),
                entry_analyst_details=(analysis or {}).get("analyst_details", {}),
            )

            # Claude 기억에 매수 기록
            if self.use_claude:
                analysis = self.strategy.get_last_analysis()
                self.strategy.analyst.memory.record_trade(
                    code, name, "buy", reason,
                    analyst_signals=analysis.get("analyst_details") if analysis else None,
                )

            if self.discord:
                self.discord.notify_trade(
                    side="long_term_buy" if is_long_term else "buy",
                    stock_name=name, stock_code=code, qty=qty, price=price,
                    reason=f"[{tranche}차/{tranche + remaining}] {reason}",
                    is_long_term=is_long_term, long_term_reason=long_term_reason,
                )

    def _handle_sell(self, code: str, name: str, force_reason: str = "", sell_ratio: float = 1.0):
        # 최소 보유 시간 체크 — 강제 매도(손절/위험)가 아닌 한, 매수 후 10분 미만이면 보류
        if not force_reason:
            last_buy = self.tracker.get_last_buy_time(code)
            if last_buy:
                elapsed = (datetime.now() - last_buy).total_seconds()
                if elapsed < 600:  # 10분
                    logger.info("%s — 매수 후 %d분 경과 (최소 10분), 매도 보류", name, int(elapsed / 60))
                    return

        balance = self.client.get_balance()
        holding = next((h for h in balance["holdings"] if h["code"] == code), None)
        if not holding or holding["qty"] <= 0:
            return

        total_qty = holding["qty"]
        price = holding["current_price"]
        avg_price = holding["avg_price"]
        reason = force_reason
        if not reason and self.use_claude:
            analysis = self.strategy.get_last_analysis()
            reason = analysis.get("reasoning", "") if analysis else ""

        # ── 분할 매도 (강제 매도가 아닐 때) ──
        if force_reason:
            #: sell_ratio<1.0이면 부분 매도 (급등 라이딩용). 기본 1.0=전량이라
            # 트레일링/손절/크래시 등 보호성 강제매도는 기존대로 전량. 급등 트림만 일부.
            if sell_ratio < 1.0:
                qty = max(1, int(total_qty * sell_ratio))
            else:
                qty = total_qty  # 트레일링/손절은 전량
        else:
            sell_plan = self.positions.plan_sell(code, price, total_qty)
            qty = sell_plan["qty"]
            if qty <= 0:
                logger.info("분할매도: %s", sell_plan.get("reason", "대기"))
                return
            reason = f"[{sell_plan['tranche']}차] {reason}"

        logger.info("▶ 매도: %s %d/%d주", name, qty, total_qty)

        if not self.order_guard.can_order(code, "sell"):
            return

        result = None
        for attempt in range(3):
            try:
                result = self.client.sell_market(code, qty)
                self.network.record_success()
                break
            except Exception as e:
                self.network.record_failure()
                logger.warning("매도 API 실패 (%d/3): %s — %s", attempt + 1, code, e)
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))

        if not result or not result.get("success"):
            logger.error("매도 최종 실패: %s %d주 — %s", name, qty, result.get("message", "") if result else "응답 없음")
            if self.discord:
                self.discord.notify_error(f"매도 실패: {name} {qty}주 — 수동 확인 필요")
            return

        self.order_guard.record_order(code, "sell", qty, price, result.get("order_no", ""))
        if result.get("success"):
            self.positions.record_sell(code, qty)
            pnl = self.tracker.record_sell(code, name, qty, price, avg_price, reason)
            self._record_sell_for_churn_guard(code, reason, sell_price=price)

            # 보상 엔진 + 애널리스트 성과 기록
            # 매도 판단의 과매수 RSI/애널리스트 의견을 진입 성과로 학습하면 청산 신호가
            # 다음 매수의 우승 패턴으로 뒤집힌다. 반드시 체결 때 보존한 진입 자료만 쓴다.
            indicators = self._last_entry_indicators(code)
            analyst_details = self._last_entry_analyst_details(code)

            # 수동 진입은 자동 전략의 산출물이 아니므로 별도 버킷으로 — 전략 EMA 오염 방지.
            # 잔금소진/강제매수도 진입 사유는 별도 ROI로 관측하되 전략 버킷은 유지한다.
            _entry_reason = self.tracker.get_last_buy_reason(code)
            _strategy_bucket = "manual" if "수동" in _entry_reason else self.strategy.name
            self.reward.record_trade_result(
                stock_code=code, stock_name=name,
                strategy_name=_strategy_bucket,
                realized_pnl=pnl["realized_pnl"],
                realized_rate=pnl["realized_rate"],
                indicators=indicators,
                # 반드시 매수 때 저장한 진입 컨텍스트로 귀속한다. 매도 패턴을 넣으면
                # rsi_overbought 익절 승률이 과매수 *매수* 승률로 역전파된다.
                context=(self._last_entry_context(code)
                         or self._build_reward_context(
                             getattr(self, "_market_condition", "peace"),
                             self._is_inverse(code))),
            )

            # 3인 애널리스트 성과 경쟁 기록
            if self.use_claude and analyst_details:
                self.strategy.analyst.record_result(
                    analyst_details, pnl["realized_rate"],
                    stock_code=code, stock_name=name,
                )

            # 이벤트 매핑 성과 기록 (어떤 이벤트로 들어간 매매인지)
            if reason and any(k in reason for k in ("이벤트", "수혜", "전쟁", "공포", "반등")):
                # 이벤트 기반 매매였음 → 성과 기록
                self.event_learner.record_event_trade(
                    "event_trade", "mixed", code,
                    pnl["realized_pnl"], pnl["realized_rate"],
                )

            if self.discord:
                self.discord.notify_trade(
                    side="sell", stock_name=name, stock_code=code, qty=qty, price=price,
                    reason=reason, realized_pnl=pnl["realized_pnl"], realized_rate=pnl["realized_rate"],
                )

            # 매도 후 다른 KR 종목 즉시 탐색
            self._rotate_kr_stock(code, proceeds=qty * price)

    def run_kr(self):
        """한국 주식 매매 (장중에만)."""
        if not getattr(self, "kr_enabled", True):
            return   # config kr_enabled=false — 한국 매매 전면 비활성
        if not self.client.is_market_open():
            return
        # 동시 실행 가드 — 개장 즉시 사이클 + 스케줄 run_once 겹침 방지 (중복 매수 차단)
        if getattr(self, "_kr_running", False):
            logger.debug("run_kr 이미 실행 중 — 중복 사이클 스킵")
            return
        self._kr_running = True
        try:
            return self._run_kr_inner()
        finally:
            self._kr_running = False

    def _run_kr_inner(self):
        if not self._check_risks_before_trading("KR"):
            logger.warning("리스크 체크 실패 — KR 매매 중단")
            return
        if not self.kr_stocks:
            logger.warning("KR 신규 진입 후보 없음 — 보유 종목 청산 관리만 계속")

        # 네트워크 체크 — 3회 실패 시 리셋 후 계속 (멈추지 않음)
        if self.network.should_pause():
            logger.warning("네트워크 불안정 — 리셋 후 계속")
            self.network._failures.clear()

        logger.info("===== KR 매매 [%s] =====", datetime.now().strftime("%H:%M:%S"))
        self._safety_scan()
        # 핵심(whitelist) 코어 진입 — Claude 분석 대기 없이 사이클 맨 앞에서 먼저 매수.
        # 변동성 장에선 모든 종목이 claude_quick(순차·느림)을 타 삼성/하이닉스가 6번째라
        # 한참 뒤에야(또는 영영) 도달하던 문제 → 로컬 코어 패스로 즉시 목표까지 확보.
        if self.kr_stocks:
            self._core_entry_pass_kr()

        def _exec_safe(stock):
            try:
                self._execute_stock(stock)
                self.network.record_success()
                return True
            except Exception as e:
                self.network.record_failure()
                logger.exception("%s 오류", stock.get("code"))
                msg = self._format_error_alert("KR", stock.get("name", stock.get("code", "?")), e)
                if self.discord and msg:
                    self.discord.notify_error(msg)
                return False

        # 보유 종목은 스크리너 선별 결과와 무관하게 항상 매도 분석 대상에 포함
        scan_kr = list(self.kr_stocks)
        kr_holdings: list[dict] = []
        cur_bal: dict = {}  # get_balance 예외 시 UnboundLocalError 방지 (5370 등에서 참조)
        try:
            cur_bal = self.client.get_balance()
            kr_holdings = cur_bal.get("holdings", [])
            seen = {s.get("code", "") for s in scan_kr}
            for h in kr_holdings:
                code = h.get("code")
                if code and code not in seen:
                    scan_kr.append({"code": code, "name": h.get("name", code)})
                    if code not in self._merge_logged_kr:
                        logger.info("KR 보유 종목 분석 대상 추가: %s(%s)", h.get("name", code), code)
                        self._merge_logged_kr.add(code)
                    seen.add(code)
        except Exception:
            logger.debug("KR 보유 병합 실패", exc_info=True)

        # 유령 포지션 정리 — positions.json 이 실잔고와 안 맞으면(record_sell 누락·코드 재사용)
        # is_pyramid_eligible/breakeven 이 오작동해 buy↔sell churn(수수료 손실, 실측 256750)을
        # 유발. 잔고 조회가 성공(휴장 빈 응답 아님)했을 때만, 실보유 없는 KR 포지션을 청산.
        if isinstance(cur_bal, dict) and cur_bal.get("cash") is not None:
            try:
                rem = self.positions.reconcile_holdings(
                    {h.get("code") for h in kr_holdings}, market="KR")
                if rem:
                    logger.warning("유령 포지션 정리(KR, 실보유 0): %s", rem)
            except Exception:
                logger.debug("KR 포지션 재조정 실패", exc_info=True)

        # 인버스 KR ETF 상시 편입 (A). 미보유 + 진입 gate 실패 시 _execute 스킵 (비용 절약)
        if self.config.get("inverse", {}).get("enabled", True):
            held_kr_codes = {h.get("code") for h in kr_holdings}
            scan_codes = {s.get("code", "") for s in scan_kr}
            for inv in self._inverse_kr_list():
                if inv["code"] in scan_codes:
                    continue
                scan_kr.append(inv)

        held_kr_codes = {h.get("code") for h in kr_holdings}
        allow_inverse, inverse_reason = self._should_allow_inverse_entry()

        # 가용 KR 현금이 최소매수금액 미만이면 미보유 종목은 분석 자체 스킵 → API 절약
        # KIS 'cash' 직접 사용 (sll_ruse 매도금 즉시 재매수 가능분 포함)
        cash_available = cur_bal.get("cash", 0)
        skip_non_held_kr = cash_available < max(self.min_amount, 5000)
        if skip_non_held_kr:
            before = len(scan_kr)
            scan_kr = [s for s in scan_kr if s.get("code") in held_kr_codes]
            skipped = before - len(scan_kr)
            if skipped > 0:
                logger.info("KR 가용 현금 %s원 < 최소매수 %s원 → 미보유 %d종 분석 스킵 (API 절약)",
                            f"{cash_available:,}", f"{self.min_amount:,}", skipped)

        # 분석 동시화: ThreadPoolExecutor로 N종목 병렬 분석/매수.
        # KIS API rate limit 18 req/sec + 매수 cash 감소는 KIS 자체 거부로 race 방지.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        candidates = [s for s in scan_kr if not (
            self._is_inverse(s.get("code", "")) and
            s.get("code", "") not in held_kr_codes and
            not allow_inverse
        )]
        # 장전 산출 우선순위가 오늘자면 그 순서로 정렬 → 개장 시 가장 좋은 후보부터 매수
        # (max_workers=1이라 순차 처리 = 우선순위 매수). 보유/미준비 종목은 뒤로.
        today = datetime.now().strftime("%Y-%m-%d")
        prio = getattr(self, "_open_priority", None)
        if prio and getattr(self, "_open_prep_date", "") == today:
            candidates.sort(key=lambda s: prio.get(s.get("code", ""), 10_000))
            logger.info("장전 우선순위 적용: 상위 %s",
                        ", ".join(s.get("name", s.get("code", "")) for s in candidates[:5]))
        #: 보유 인버스 최우선 슬롯 (stable sort — 나머지 순서 유지).
        # 레짐 종료 청산이 종목 순차 처리에 밀려 개장 +6분에 실행, 갭업장에서 인버스가
        # 흘러내리는 동안 대기한 실측(09:00→09:06). 개장 직후 = 변동 최대 구간이므로
        # 청산 판단을 첫 슬롯으로. 재하락 반전 시 진입 게이트(1차 즉시)가 다시 산다.
        candidates.sort(key=lambda s: 0 if (s.get("code", "") in held_kr_codes
                                            and self._is_inverse(s.get("code", ""))) else 1)
        max_workers = self.config.get("analysis_max_workers", 4)
        logger.info("KR 분석 병렬: %d종목 / %d workers", len(candidates), max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_exec_safe, s): s for s in candidates}
            for f in as_completed(futures):
                try:
                    f.result(timeout=180)
                except Exception:
                    pass
