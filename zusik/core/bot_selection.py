from __future__ import annotations

import logging
import os
from datetime import datetime


logger = logging.getLogger(__name__)


class SelectionMixin:
    """SelectionMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    @staticmethod
    def _screen_symbol(stock: dict, market: str) -> str:
        return stock.get("code", "") if market == "KR" else stock.get("ticker", "")

    def _screening_authority(self) -> str:
        """감시목록을 실제로 바꿀 단일 선별기 이름."""
        authority = str((self.config.get("screening", {}) or {}).get(
            "authority", "auto"
        )).strip().lower()
        return authority if authority in ("auto", "llm", "hybrid") else "auto"

    def _screen_session_locked(self, market: str) -> bool:
        """장중 감시목록 교체를 막아 한 세션 동안 진입 유니버스를 고정한다."""
        cfg = self.config.get("screening", {}) or {}
        if not cfg.get("session_lock_enabled", True):
            return False
        try:
            return (bool(self.client.is_market_open()) if market == "KR"
                    else bool(self.client.is_us_market_open()))
        except Exception:
            return True  # 장 상태를 모르면 목록 변경보다 안정성 우선

    def _apply_pending_screened_if_unlocked(self) -> None:
        """장중 대기시킨 결과를 해당 시장 폐장 후 다음 세션용으로 적용한다."""
        pending = getattr(self, "_pending_screened_results", {}) or {}
        if not pending:
            return
        ready = {}
        for market, key in (("KR", "kr"), ("US", "us")):
            if pending.get(key) and not self._screen_session_locked(market):
                ready[key] = pending.pop(key)
        self._pending_screened_results = pending
        if ready:
            self._apply_screened_stocks(ready, tag="세션 대기 선별 적용", force=True)

    def _stable_ranked_selection(self, current: list[dict], proposed: list[dict],
                                 market: str) -> list[dict]:
        """기존 강세 후보를 유지하고 의미 있게 우수한 신규 후보만 제한 교체한다."""
        cfg = self.config.get("screening", {}) or {}
        if not cfg.get("stability_gate_enabled", True):
            return self._rank_by_relative_strength(proposed, market)

        by_symbol = {}
        # 신규 메타(reason/screen_score)를 우선하되 기존 종목도 함께 재평가한다.
        for stock in current:
            sym = self._screen_symbol(stock, market)
            if sym:
                by_symbol[sym] = dict(stock)
        proposed_symbols = set()
        for stock in proposed:
            sym = self._screen_symbol(stock, market)
            if sym:
                proposed_symbols.add(sym)
                merged = dict(by_symbol.get(sym, {}))
                merged.update(stock)
                by_symbol[sym] = merged

        ranked = self._rank_by_relative_strength(list(by_symbol.values()), market)
        if not ranked:
            return []
        target_key = "kr_count" if market == "KR" else "us_count"
        target = max(1, int(cfg.get(target_key, len(proposed) or len(current) or 1)))
        max_replacements = max(0, int(cfg.get("max_replacements_per_update", 3)))
        min_gain = float(cfg.get("min_score_improvement", 0.02))
        old_symbols = {self._screen_symbol(s, market) for s in current}

        eligible_old = [s for s in ranked if self._screen_symbol(s, market) in old_symbols]
        selected = eligible_old[:target]
        selected_symbols = {self._screen_symbol(s, market) for s in selected}
        newcomers = [
            s for s in ranked
            if self._screen_symbol(s, market) in proposed_symbols
            and self._screen_symbol(s, market) not in selected_symbols
        ]

        # RS 게이트를 통과한 기존 후보가 부족하면 빈 슬롯은 즉시 채운다.
        while newcomers and len(selected) < target:
            item = newcomers.pop(0)
            selected.append(item)
            selected_symbols.add(self._screen_symbol(item, market))

        replacements = 0
        while newcomers and selected and replacements < max_replacements:
            best_new = newcomers[0]
            worst_old = min(selected, key=lambda s: float(s.get("_selection_score", -999)))
            new_score = float(best_new.get("_selection_score", -999))
            old_score = float(worst_old.get("_selection_score", -999))
            if new_score < old_score + min_gain:
                break
            newcomers.pop(0)
            selected.remove(worst_old)
            selected.append(best_new)
            replacements += 1

        order = {self._screen_symbol(s, market): i for i, s in enumerate(ranked)}
        selected.sort(key=lambda s: order.get(self._screen_symbol(s, market), 10**9))
        if replacements:
            logger.info("%s 감시목록 안정화: 점수 +%.3f 이상 신규만 %d종 교체",
                        market, min_gain, replacements)
        return selected

    def _load_stocks(self):
        """저장된 선별 결과가 있으면 로드, 없으면 config 기본값."""
        if self.screener and self.auto_screen:
            selected = self.screener.get_selected()
            if selected.get("kr") and getattr(self, "kr_enabled", True):
                self.kr_stocks = self._filter_derivatives(selected["kr"], market="KR")
            else:
                self.kr_stocks = list(self._default_kr)   # kr_enabled=false 면 [] (기본 풀이 비어있음)
            if selected.get("us") and getattr(self, "us_enabled", True):
                self.us_stocks = self._filter_derivatives(selected["us"], market="US")
            else:
                self.us_stocks = list(self._default_us)   # us_enabled=false 면 [] (기본 풀이 비어있음)
        else:
            self.kr_stocks = list(self._default_kr)
            self.us_stocks = list(self._default_us)

        self.stocks = self.kr_stocks  # 하위 호환

    def _refresh_stocks(self, force: bool = False, market: str = "auto"):
        """Claude로 종목 재선별.

        평시: screen_interval_hours마다 (기본 2시간)
        위기: crisis_interval_minutes마다 (기본 30분)
        force=True: 즉시 재선별
        market: "auto"(열린 장만) / "kr" / "us" / "both"
          - US 장만 열렸을 때 KR도 재선별되어 KR 종목 교체
            알림이 장 닫힌 시간에 날아가던 버그 방지
        """
        if not self.screener or not self.auto_screen:
            return
        if self._screening_authority() not in ("llm", "hybrid"):
            logger.debug("LLM 종목 선별 스킵 — 단일 권위=%s", self._screening_authority())
            return
        if not force and not self.screener.needs_update():
            return

        # 장 상태에 따라 실제 재선별할 시장 결정
        if market == "auto":
            do_kr = False
            do_us = False
            try:
                do_kr = self.client.is_market_open()
                do_us = self.client.is_us_market_open()
            except Exception:
                pass
        elif market == "both":
            do_kr = do_us = True
        else:
            do_kr = market == "kr"
            do_us = market == "us"

        if not do_kr and not do_us:
            logger.debug("종목 재선별 스킵 — KR/US 둘 다 휴장")
            return

        # 현금 고갈 시 재선별 무의미 — Claude 호출 낭비 방지 (force=True는 강제 허용)
        if not force:
            try:
                # KIS의 'cash'(orderable_cash)는 sll_ruse 재사용 가능 매도금까지 반영하므로
                # _get_unsettled_kr_cash 별도 차감 시 중복 — 한국 시장은 매도 즉시 재매수 가능
                kr_cash_left = self.client.get_balance().get("cash", 0)
            except Exception:
                kr_cash_left = 0
            try:
                us_cash_left = float(self.client.get_us_balance().get("cash_usd", 0) or 0)
            except Exception:
                us_cash_left = 0.0
            kr_can_buy = kr_cash_left >= max(self.min_amount, 5000)
            us_can_buy = us_cash_left >= self.min_amount_usd
            if do_kr and not kr_can_buy:
                do_kr = False
            if do_us and not us_can_buy:
                do_us = False
            if not do_kr and not do_us:
                logger.info("종목 재선별 스킵 — 가용 현금 부족 (KR %s원, US $%.2f)",
                            f"{kr_cash_left:,}", us_cash_left)
                return

        mode = "위기" if self.screener.is_crisis_mode() else "정상"
        logger.info("══ Claude 종목 선별 [%s] — KR=%s US=%s ══",
                    mode, "on" if do_kr else "off", "on" if do_us else "off")
        perf = self.reward.get_performance_summary_text()

        try:
            max_kr = 0
            max_usd = 0.0
            try:
                bal = self.client.get_balance()
                total_kr = bal["cash"] + bal["total_eval"]
                max_kr = max(total_kr // 2, bal["cash"])
                us_bal = self.client.get_us_balance()
                max_usd = us_bal.get("cash_usd", 0) + sum(
                    h.get("qty", 0) * h.get("current_price", 0)
                    for h in us_bal.get("holdings", [])
                )
            except Exception:
                pass
            if max_kr > 0:
                logger.info("종목 선별 가격 제한: KR %s원 이하, US $%.0f 이하", f"{max_kr:,}", max_usd)

            #: config.reports.claude_screener_enabled=false면 Claude 종목 선별 스킵
            # auto_screener (MC + Vortex, Claude 미사용)만 사용해도 충분
            if not self.config.get("reports", {}).get("claude_screener_enabled", True):
                logger.info("Claude 종목 선별 스킵 (claude_screener_enabled=false). auto_screener만 사용")
                return
            result: dict = {}
            if do_kr:
                result["kr"] = self.screener.screen_kr_stocks(
                    self.kr_stocks, performance_summary=perf, max_price_krw=max_kr,
                )
            if do_us:
                result["us"] = self.screener.screen_us_stocks(
                    self.us_stocks, performance_summary=perf, max_price_usd=max_usd,
                )
            self._apply_screened_stocks(result, tag="종목 선별", force=force)

            #: 분기 호출(screen_kr/us_stocks)은 selected_stocks.json의
            # last_updated를 갱신하지 않아 needs_update가 매번 True → 2분마다
            # Claude 재호출되던 버그 수정. 여기서 명시적으로 캐시·파일 갱신.
            try:
                from zusik.analysis.stock_screener import _save_selected
                if "kr" in result:
                    self.screener._selected["kr"] = result["kr"]
                if "us" in result:
                    self.screener._selected["us"] = result["us"]
                self.screener._selected["last_updated"] = datetime.now().isoformat()
                _save_selected(self.screener._selected)
            except Exception:
                logger.debug("screener 캐시 갱신 실패", exc_info=True)
        except Exception:
            logger.exception("종목 선별 실패, 기존 유지")

    def _check_stock_price(self, code: str, max_price: int) -> bool:
        """종목 현재가가 max_price 이하인지 확인. 조회 실패 시 True (통과)."""
        try:
            p = self.client.get_current_price(code)
            return p["price"] <= max_price
        except Exception:
            return True  # 조회 실패 시 일단 통과

    def _apply_screened_stocks(self, result: dict, tag: str = "종목 교체",
                               force: bool = False):
        """선별 결과를 실제 종목 리스트에 적용 (가격 필터 포함)."""
        result = dict(result or {})
        if not force:
            pending = getattr(self, "_pending_screened_results", {}) or {}
            for market, key in (("KR", "kr"), ("US", "us")):
                if result.get(key) and self._screen_session_locked(market):
                    pending[key] = list(result[key])
                    result.pop(key, None)
                    logger.info("%s %s 결과 대기 — 장중 감시목록 고정, 폐장 후 적용",
                                tag, market)
            self._pending_screened_results = pending
        if not result:
            return

        old_kr = {s.get("code", s.get("ticker", "")) for s in self.kr_stocks}
        old_us = {s.get("ticker", "") for s in self.us_stocks}

        # 잔고 기반 가격 필터 — 매수 불가 종목 제거
        try:
            bal = self.client.get_balance()
            max_kr_price = max(bal["cash"], bal["cash"] + bal["total_eval"]) // 2
            if max_kr_price > 0 and result.get("kr"):
                before = len(result["kr"])
                result["kr"] = [s for s in result["kr"]
                                if self._check_stock_price(s.get("code", ""), max_kr_price)]
                if len(result["kr"]) < before:
                    logger.info("종목 선별 가격 필터: KR %d→%d종목 (max %s원)",
                                before, len(result["kr"]), f"{max_kr_price:,}")
        except Exception:
            pass

        if result.get("kr") and getattr(self, "kr_enabled", True):
            self.kr_stocks = self._stable_ranked_selection(
                self.kr_stocks,
                self._filter_derivatives(result["kr"], market="KR"), "KR")
        if result.get("us") and getattr(self, "us_enabled", True):
            self.us_stocks = self._stable_ranked_selection(
                self.us_stocks,
                self._filter_derivatives(result["us"], market="US"), "US")
        self.stocks = self.kr_stocks

        new_kr = {s.get("code", "") for s in self.kr_stocks}
        new_us = {s.get("ticker", "") for s in self.us_stocks}

        for s in self.kr_stocks:
            code = s.get("code", "")
            # LLM 선별이 준 이름은 환각/스테일일 수 있어 KIS 권위 이름으로 교정(get_stock_name 캐시라 1회 비용).
            # 실측: 256750 을 잘못된 이름으로 부르면 위험탐지 LLM 이 다른 종목 뉴스를 끌어와 오판.
            try:
                kis_name = self.client.get_stock_name(code)
                if kis_name and kis_name != code and kis_name != s.get("name"):
                    logger.info("종목명 교정(KIS 권위): %s '%s' → '%s'", code, s.get("name"), kis_name)
                    s["name"] = kis_name
            except Exception:
                pass
            if s.get("name"):
                self._name_cache[code] = s["name"]
        for s in self.us_stocks:
            if s.get("name"):
                self._name_cache[s.get("ticker", "")] = s["name"]

        added_kr = new_kr - old_kr
        removed_kr = old_kr - new_kr
        added_us = new_us - old_us
        removed_us = old_us - new_us

        # 입력 후보는 있었지만 RS 게이트에서 전부 탈락한 경우 기본 목록을 되살리지 않는다.
        # 그 폴백이 약한 원본을 다시 매수 유니버스로 넣어 상대강도 필터를 무효화했다.
        # 보유 종목은 run_kr/run_us에서 별도로 병합하므로 청산 관리는 계속된다.
        if result.get("kr") and not self.kr_stocks:
            logger.warning("KR 후보 전원 상대강도 미달 — 신규 진입 후보 없음")
        if result.get("us") and not self.us_stocks:
            logger.warning("US 후보 전원 상대강도 미달 — 신규 진입 후보 없음")

        if added_kr or removed_kr or added_us or removed_us:
            logger.info("%s — KR +%d/-%d, US +%d/-%d",
                        tag, len(added_kr), len(removed_kr), len(added_us), len(removed_us))

            if self.discord:
                changes = []
                if added_kr:
                    changes.append(f"KR 추가: {', '.join(added_kr)}")
                if removed_kr:
                    changes.append(f"KR 제거: {', '.join(removed_kr)}")
                if added_us:
                    changes.append(f"US 추가: {', '.join(added_us)}")
                if removed_us:
                    changes.append(f"US 제거: {', '.join(removed_us)}")

                kr_list = "\n".join(
                    f"  {s.get('name', s.get('code', ''))} — {s.get('reason', '')[:50]}"
                    for s in self.kr_stocks
                )
                us_list = "\n".join(
                    f"  {s.get('name', s.get('ticker', ''))} — {s.get('reason', '')[:50]}"
                    for s in self.us_stocks
                )
                self.discord.notify_stock_rotation("\n".join(changes), kr_list, us_list)

    def _execute_stock(self, stock: dict):
        code = stock["code"]
        name = stock.get("name") or self._get_name(code)

        # ── 현금 사전 체크: 미보유 + 매수 여력 없음 → 분석 스킵 ──
        # 보유 중이면 매도 판단 위해 계속 분석
        if not self.positions.has_position(code):
            try:
                cur_cash = self.client.get_balance().get("cash", 0)
                cur_price = self.client.get_current_price(code).get("price", 0)
                if cur_cash < cur_price or cur_cash < self.min_amount:
                    logger.debug("%s 분석 스킵 — 미보유 + 현금 부족 (현금 %s, 주가 %s)",
                                 name, f"{cur_cash:,}", f"{cur_price:,}")
                    return
            except Exception:
                pass

        df = self.client.get_ohlcv(code, period=self.period)
        if df is None or df.empty:
            return

        # 시장 전체 crisis 감지는 `_check_risks_before_trading`의 detect_market_condition이
        # 담당. 개별 종목 OHLCV로 crisis 판정하면 특정 종목의 단기 급락을 전체 시장 위기로
        # 오인해 5분마다 긴급 홀딩 발동/해제를 반복하는 버그 발생.
        if self.risk.is_emergency_hold():
            return

        price_info = self.client.get_current_price(code)
        price = price_info["price"]
        intraday_change = price_info.get("change_rate", 0) / 100  # 장중 변동률
        self._last_intraday_change[code] = intraday_change  # 매수 게이트가 참조

        # 시장 온도 업데이트
        self.cost.update_market_temperature(intraday_change)

        # ── 장중 급변 감지 (현재가 API만으로, OHLCV 불필요) ──
        if abs(intraday_change) >= 0.03:
            logger.warning("%s 장중 급변: %+.1f%%", name, intraday_change * 100)

        # ── 인버스 ETF는 전용 헷지 핸들러로 라우팅 ──
        # 모멘텀/RSI 분석기는 급등한 인버스를 '과매수=SELL'로 봐 매수 경로 진입조차 막는다
        # (폭락일 인버스 매수 0건의 근본 원인). 분석기를 건너뛰고 직접 헷지 판단한다.
        if self._is_inverse(code):
            self._handle_inverse(code, name, price, df)
            return

        # ── 보유 종목 자동 대응 (API 비용 $0, 즉시 반응) ──
        if self.positions.has_position(code):
            # 급락 감지 (최우선 — 1ms도 아까움)
            crash = self.positions.check_crash(code, price, df)
            # 조기손절 억제, US 승자보유 패턴 이식): 정상 pullback 구간은 홀드.
            # 깊은 붕괴(crash_from_high -20% / -15%↓)·펀더멘털 위험만 매도. crash_instant
            # 0%승률(0/13, -649k 바닥투매) 주범 제거. 핵심주 -15%, 비핵심 KR -9%(config)까지 홀드.
            if crash:
                _hb = self.client.get_balance()  # 5초 TTL 캐시
                _hh = next((h for h in _hb["holdings"] if h["code"] == code), None)
                _hpr = ((_hh.get("profit_rate", 0) or 0) / 100) if _hh else 0.0
                _deep = (crash.get("action") == "crash_from_high"
                         or crash.get("change", 0) <= -0.15)
                if self._hold_through_loss(code, _hpr, deep_collapse=_deep):
                    logger.info("급락 홀드(조기손절 억제): %s 손익 %+.1f%% (급락 %+.1f%%) — pullback 보류, 깊은붕괴/하드스톱만 매도",
                                name, _hpr * 100, crash.get("change", 0) * 100)
                    crash = None  # 매도 스킵, 아래 surge/trailing 로직은 계속
            if crash:
                # 매수 직후 30분 보호 — RIOT 4/29 buy→sell 1분 churn 방지
                # 방금 산 종목을 -7% 하락으로 즉시 던지면 손실 확정. 적어도 30분 관망
                last_buy_recent = self._is_recently_bought(code, minutes=30)
                if last_buy_recent:
                    logger.warning("급락 감지했으나 매수 직후 보호기간 — 매도 보류: %s %+.1f%%",
                                   name, crash["change"] * 100)
                    return
                balance = self.client.get_balance()
                holding = next((h for h in balance["holdings"] if h["code"] == code), None)
                if holding and holding["qty"] > 0:
                    qty = max(1, int(holding["qty"] * crash["sell_ratio"]))
                    logger.critical("급락 대응: %s %+.1f%% → %s (%d주)",
                                    name, crash["change"] * 100, crash["action"], qty)
                    self._handle_sell(code, name, force_reason=f"급락 손절: {crash['reason']}")
                    return

            # 급등 감지 (트레일링보다 먼저 체크)
            surge = self.positions.check_surge(code, price, df)
            if surge:
                sell_ratio = surge["sell_ratio"]
                balance = self.client.get_balance()
                holding = next((h for h in balance["holdings"] if h["code"] == code), None)
                if holding and holding["qty"] > 0:
                    qty = max(1, int(holding["qty"] * sell_ratio))
                    logger.info("급등 대응: %s +%.0f%% → %s (%d주, ratio %.2f)",
                                name, surge["profit_rate"] * 100, surge["action"], qty, sell_ratio)
                    #: surge sell_ratio 실제 반영 — 절반익절/라이딩 트림이
                    # force_reason 때문에 100% 전량 매도되던 버그 수정 (큰 추세 놓침).
                    self._handle_sell(code, name, force_reason=surge["reason"], sell_ratio=sell_ratio)
                    if self.discord:
                        self.discord.notify_trade(
                            "sell", name, code, qty, price,
                            reason=surge["reason"],
                        )
                    return

            # 트레일링 스톱 + 본전 보호
            trailing = self.positions.update_trailing_stop(code, price)
            if trailing:
                if trailing.get("action") == "stop_triggered":
                    logger.info("트레일링 스톱: %s 고점 %s → %s", name, f"{trailing['high']:,}", f"{price:,}")
                    self._handle_sell(code, name, force_reason="트레일링 스톱")
                    return
                if trailing.get("action") == "breakeven_protect":
                    peak = trailing.get("peak_profit", 0) * 100
                    # 핵심주는 본전보호(소액 give-back) 매도 면제 — 장기 코어인데 +3%→+0% 되돌림에
                    # 던지면 코어 패스가 즉시 재매수해 churn 루프(현대차 사례). 큰 추세는 트레일링/surge가 처리.
                    if self._core_hold_through(code):
                        logger.info("핵심주 %s 본전보호 면제 — 코어 홀드 (churn 방지)", name)
                    else:
                        logger.info("본전 보호: %s 최고 +%.1f%% → 현재 %s, 수익 소멸 방지 익절", name, peak, f"{price:,}")
                        self._handle_sell(code, name, force_reason=f"본전 보호 (최고 +{peak:.1f}% 수익 소멸)")
                        return

            # 로컬 빠른 트리거 (LLM 호출 없이 즉시) — 5/1 추가
            # 변동성 + 시장 상황 자동 분류 → 일봉/5분봉/1분봉/틱 선택적 적용
            import zusik.core.volatility_classifier as vc
            balance_for_rsi = self.client.get_balance()
            holding_for_rsi = next((h for h in balance_for_rsi["holdings"]
                                    if h["code"] == code), None)
            if holding_for_rsi and holding_for_rsi.get("qty", 0) > 0:
                pr = (holding_for_rsi.get("profit_rate", 0) or 0) / 100
                tier_info = vc.classify(
                    df,
                    market_condition=getattr(self, "_market_condition", "peace"),
                    holding=True,
                )
                logger.debug("KR %s 변동성: %s", name, tier_info["reason"])

                rsi_exit = None
                quick_loss = None

                def _check_exit(_df_local, label):
                    nonlocal rsi_exit, quick_loss
                    if not rsi_exit:
                        r = self.signals.check_overbought_exit(_df_local, profit_rate=pr, rsi_min=self._adaptive_params().get("rsi_exit_min", 80), profit_min=self._adaptive_params().get("rsi_exit_profit_min", 0.03))
                        if r:
                            r["reason"] = f"[{label}] " + r["reason"]
                            rsi_exit = r
                    if not quick_loss:
                        q = self.signals.check_quick_loss_exit(_df_local, profit_rate=pr)
                        if q:
                            q["reason"] = f"[{label}] " + q["reason"]
                            quick_loss = q

                # 일봉은 항상 체크
                _check_exit(df, "일봉")
                # 5분봉: medium tier 이상
                if not rsi_exit and not quick_loss and tier_info["use_minute_5"]:
                    try:
                        m5 = self.client.get_minute_ohlcv(code, minutes=5)
                        if m5 is not None and len(m5) >= 21:
                            _check_exit(m5, "5분봉")
                    except Exception:
                        pass
                # 1분봉: high tier 이상
                if not rsi_exit and not quick_loss and tier_info["use_minute_1"]:
                    try:
                        m1 = self.client.get_minute_ohlcv(code, minutes=1)
                        if m1 is not None and len(m1) >= 21:
                            _check_exit(m1, "1분봉")
                    except Exception:
                        pass
                # WebSocket: extreme tier — 별도 구독은 _maintain_websocket이 관리
                if tier_info["use_websocket"]:
                    self._ensure_ws_subscription(code, market="KR")

                if rsi_exit:
                    # 분할 트림: 에피소드당 절반 익절 + 절반 라이딩 (적시성 27% 개선)
                    trim = self._rsi_trim_ratio(code)
                    if trim is None:
                        logger.info("RSI 과매수 유지 — 트림 완료, 잔여 라이딩 (트레일링 관리): %s", name)
                    else:
                        logger.info("RSI 과매수 익절: %s — %s (tier=%s, trim %.0f%%)",
                                    name, rsi_exit["reason"], tier_info["tier"], trim * 100)
                        self._handle_sell(code, name, force_reason=rsi_exit["reason"], sell_ratio=trim)
                        self.positions.mark_rsi_trimmed(code)  # 잔여 라이딩분 본전보호 개시
                        return
                if quick_loss:
                    #: 매수 직후 30분 보호 (US 경로와 동일) — buy→즉시 손절 churn 방지.
                    #: 조기손절 억제 — 핵심 -15% / 비핵심 KR -9%(config)까지 pullback 홀드.
                    if self._hold_through_loss(code, pr):
                        logger.info("빠른손절 보류(조기손절 억제): %s 손익 %+.1f%% — pullback 홀드", name, pr * 100)
                    elif self._is_recently_bought(code, minutes=30):
                        logger.info("빠른손절 신호나 매수 직후 보호기간 — 보류: %s", name)
                    else:
                        logger.warning("빠른 손절: %s — %s (tier=%s)",
                                       name, quick_loss["reason"], tier_info["tier"])
                        self._handle_sell(code, name, force_reason=quick_loss["reason"])
                        return

            # 느린 출혈 감지 (급락은 아니지만 꾸준히 밀림)
            #: "지독한 출혈 조기 손절" 사용자 요청. 5/7 강화한 -4%/0.7을 일부 되돌림.
            # min_cum_drop 0.04 → 0.025 (-4% → -2.5% 누적, 더 일찍 감지)
            # min_down_ratio 0.7 → 0.65 (5일 중 강도 약간 완화해 감지 빈도 증가)
            # profit_rate 임계 -3% → -2% (출혈 초입에 끊어 큰 손실 방지)
            # 매수 직후 30분 보호는 유지 (churn 방지)
            from zusik.analysis.indicators import slow_bleed
            bleed = slow_bleed(df, lookback=5, min_down_ratio=0.65, min_cum_drop=0.025)
            if bleed["is_bleeding"] and not self._is_recently_bought(code, minutes=30):
                balance = self.client.get_balance()
                holding = next((h for h in balance["holdings"] if h["code"] == code), None)
                if holding and holding.get("profit_rate", 0) <= -2.0:
                    pr = holding.get("profit_rate", 0)
                    # 억제: 핵심 -15% / 비핵심 KR -9%(config)까지 출혈 홀드.
                    # slow_bleed 0%승률(한화에어로 -69k 등 회복 종목 바닥투매) 방지.
                    if self._hold_through_loss(code, pr / 100.0):
                        logger.info("느린출혈 홀드(조기손절 억제): %s 손익 %.1f%% — pullback 홀드, 하드스톱만 매도", name, pr)
                    else:
                        logger.warning("느린 출혈 감지: %s 5일 중 %.0f%% 음봉, 누적 %+.1f%%, 수익률 %+.1f%% → 매도",
                                       name, bleed["down_ratio"] * 100, bleed["cum_drop"] * 100, pr)
                        self._handle_sell(code, name, force_reason=f"느린 출혈 (5일 누적 {bleed['cum_drop']:+.1%})")
                        return

            # 회전 청산: 72h+ 모멘텀 소멸 포지션이 본전 회복하면 강세에 팔아
            # 자본 재배치. 손실 실현 없음 (얕은 컷 41% 정확도 — 바닥투매 금지 원칙 유지).
            if not self._is_recently_bought(code, minutes=30):
                _rb = self.client.get_balance()  # 5초 TTL 캐시
                _rh = next((h for h in _rb["holdings"] if h["code"] == code), None)
                if _rh:
                    _rpr = (_rh.get("profit_rate", 0) or 0) / 100.0
                    if self._check_stale_rotate(code, _rpr, df):
                        logger.info("회전 청산: %s 72h+ 모멘텀 소멸, 본전 회복(%+.1f%%) — 자본 재배치", name, _rpr * 100)
                        self._handle_sell(code, name, force_reason="회전 청산 — 72h+ 모멘텀 소멸, 본전 회복 자본 재배치")
                        return

        # ══ 선제 매수 시그널 (API 비용 $0, 로컬) ══

        # 과매도 반등 사냥
        bounce = self.signals.check_oversold_bounce(df, code, name)
        if bounce and not self.positions.has_position(code):
            logger.info("반등 사냥 감지: %s — %s", name, bounce["reason"])
            # Claude 분석 건너뛰고 직접 매수 (시간이 생명)
            self._handle_buy(code, name, price, df, is_long_term=False)
            return

        # ══ 비용 최적화 게이트 ══

        # 1단계: 로컬 퀀트 사전 필터 (API 비용 0)
        local_check = self.cost.local_quick_check(df)

        # 2단계: 캐시 + 가격 변동 체크
        analysis_check = self.cost.should_analyze(code, price)

        if not analysis_check["should_call"] and not local_check["action_needed"]:
            return  # 캐시 유효 + 특이사항 없을 때만 스킵

        # 3단계: 호출할 애널리스트 선별
        call_level = analysis_check["call_level"]
        selected_analysts = self.cost.select_analysts(call_level, local_check["signal_hint"])

        logger.info("─── %s(%s) [%s분석, %d명] ───", name, code, call_level, len(selected_analysts))

        # 멀티 타임프레임 — 급변 시에만 시간봉 호출 (평시는 일봉만)
        df_hourly = None
        volatility = abs((df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2]) if len(df) >= 2 else 0
        if volatility >= 0.02:  # 2%+ 변동 시에만 시간봉
            try:
                df_hourly = self.client.get_minute_ohlcv(code, minutes=60)
            except Exception:
                pass
        mtf = self.positions.multi_timeframe_check(df, df_hourly)

        # ── 전략에 종목 + 보유 현황 전달 ──
        # kr_holdings는 use_claude=False거나 예외 발생 시에도 1538줄에서 참조되므로
        # 반드시 기본값 []로 초기화 (UnboundLocalError 방지,
        kr_holdings: list[dict] = []
        holdings_text = ""
        self.strategy.set_stock(code, name)
        if self.use_claude:
            # 매 분석마다 실시간 보유 현황 조회 → Claude에 전달
            try:
                bal = self.client.get_balance()
                kr_holdings = bal.get("holdings", [])
                cash = bal["cash"]
                if not kr_holdings:
                    holdings_text = f"현재 보유 종목 0개. 현금 {cash:,}원. 매수/관망 중립적으로 판단하세요."
                else:
                    items = [f"{h['name']} {h['qty']}주({h['profit_rate']:+.1f}%)" for h in kr_holdings]
                    holdings_text = f"보유: {', '.join(items)}. 현금: {cash:,}원."
            except Exception:
                holdings_text = "보유 현황 조회 실패"

            perf_info = self.reward.get_performance_summary_text()
            mtf_info = f"일봉={mtf['daily_trend']}, 시간봉={mtf['hourly_timing']}"

            # 모멘텀 돌파 힌트 (Claude에 선제 신호 전달)
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

            portfolio_info = f"{holdings_text} | {perf_info} | {mtf_info}"
            if breakout_hint:
                portfolio_info += f" | {breakout_hint}"

            lt_holdings = self.tracker.get_long_term_holdings()
            lt_info = ""
            if lt_holdings:
                lt_total = self.tracker.get_long_term_total_cost()
                lt_info = f"장기투자 {len(lt_holdings)}종목, {lt_total:,}원"
            # MC 통계 — LLM 분석에 통계적 근거 제공 (Vortex 8x 가속, ~80ms)
            mc_stats = self._compute_mc_stats(df, n_paths=10000, t_forward=30)
            self._last_mc_stats = mc_stats
            mc_info = self._format_mc_for_llm(mc_stats) if mc_stats else ""
            try:
                self.strategy.set_context(portfolio_info=portfolio_info,
                                          long_term_info=lt_info,
                                          mc_info=mc_info)
            except TypeError:
                # 구버전 strategy — mc_info 미지원
                self.strategy.set_context(portfolio_info=portfolio_info,
                                          long_term_info=lt_info)

            # auto_hybrid에 포지션 상태 주입 (손실 중이면 더 자주 재분석)
            if hasattr(self.strategy, "set_position_state"):
                this_holding = next((h for h in kr_holdings if h.get("code") == code), None)
                pr = (this_holding.get("profit_rate", 0) / 100) if this_holding else 0.0
                pos = self.positions._get_position(code) if self.positions.has_position(code) else {}
                from zusik.analysis.indicators import slow_bleed as _slow_bleed
                bleed_info = _slow_bleed(df) if this_holding else {"is_bleeding": False}
                self.strategy.set_position_state(
                    holding=bool(this_holding),
                    profit_rate=pr,
                    is_bleeding=bleed_info["is_bleeding"],
                    peak_profit=pos.get("peak_profit_rate", 0.0),
                )

            # 선별 호출: call_level에 따라 모델/웹검색 결정
            self.strategy.analyst._selected_roles = selected_analysts
            self.strategy.analyst._call_level = call_level

            # 비용 기록 (분석 후 claude_client에서 자동 기록됨)

        signal = self.strategy.analyze(df)

        # 결과 캐시 + 아레나 기록
        if self.use_claude:
            analysis = self.strategy.get_last_analysis()
            self.cost.cache_result(code, analysis or {"signal": signal}, price)

            # 각 에이전트 신호를 가상 포트폴리오에 기록 (수동 모니터링/성과추적 전용).
            details = analysis.get("analyst_details", {}) if analysis else {}
            for agent, d in details.items():
                self.arena.record_signal(
                    agent, code, d.get("signal", "hold"),
                    price, invest_ratio=0.15,
                )
            #: 아레나 '리더 신호 오버라이드' 제거.
            # 근거(arena.json 1년 paper-trading): 에이전트 대다수가 손실(quant -12,935,
            # bollinger -12,200, rsi -7,880…). 그 '리더'가 성과가중 4인 합의를 덮어쓰는 건
            # 자주 '손실 에이전트에게 핸들을 넘기는' 셈 → 수익에 해로움. 합의 신호를 그대로 신뢰.
            # 아레나는 이제 의사결정에 관여하지 않고 모니터링 데이터만 수집한다.

        # 신호 진동 가드 — 같은 종목 6h 내 BUY ↔ SELL 뒤집힘 차단
        _hyst_conf = 0.5
        if self.use_claude:
            _hyst_analysis = self.strategy.get_last_analysis() or {}
            _hyst_conf = float(_hyst_analysis.get("confidence", 0.5) or 0.5)
        _eff_sig, _hyst_msg = self._apply_hysteresis(code, signal, _hyst_conf)
        if _eff_sig != signal:
            logger.info("%s 신호 가드: %s", name, _hyst_msg)
            signal = _eff_sig

        label = {"buy": "단기매수", "long_term_buy": "장기매수", "sell": "매도", "hold": "관망"}.get(signal, signal)
        {"buy": "", "long_term_buy": "", "sell": "", "hold": ""}.get(signal, "")
        temp = self.cost.get_market_temperature()
        logger.info("%s %s원 (%+.2f%%) → %s | 시장:%s 캐시:%d분",
                     name, f"{price:,}", price_info["change_rate"], label,
                     temp["temperature"], temp["cache_ttl"])

        # Discord 알림은 매수/매도 시에만 (hold는 안 보냄)
        # 매수/매도 알림은 _handle_buy/_handle_sell에서 처리됨

        # 보유 0인데 SELL = 의미없음 → 다른 종목 매수 기회로 전환
        try:
            bal_check = self.client.get_balance()
            _holdings_now = bal_check.get("holdings", [])
            this_holding_info = next((h for h in _holdings_now if h.get("code") == code), None)
            has_this_stock = this_holding_info is not None
        except Exception:
            this_holding_info = None
            has_this_stock = False

        # 인버스 강제 청산: 평시 복귀 + 하락 완화 시 전략 신호 무관하게 즉시 매도
        if has_this_stock and self._is_inverse(code):
            force_exit, exit_reason = self._should_force_exit_inverse()
            if force_exit:
                logger.info("인버스 강제 청산 %s: %s", name, exit_reason)
                self._handle_sell(code, name, force_reason=exit_reason)
                return

        if signal in ("buy", "long_term_buy"):
            if mtf["daily_trend"] == "down" and mtf["hourly_timing"] == "wait":
                logger.info("멀티TF 경고: 일봉 하락+시간봉 대기 (매수 계속)")
            self._handle_buy(code, name, price, df, is_long_term=(signal == "long_term_buy"), mtf=mtf)
        elif signal == "sell" and has_this_stock:
            _sell_conf = 1.0
            if self.use_claude:
                _sell_a = self.strategy.get_last_analysis() or {}
                _sell_conf = float(_sell_a.get("confidence", 1.0) or 1.0)
            defer, reason = self._should_defer_sell(
                "KR", df,
                this_holding_info.get("qty", 0),
                this_holding_info.get("avg_price", 0),
                float(df["close"].iloc[-1]),
                confidence=_sell_conf,
            )
            # 모호(pop-then-fade) 익절 구간이면 LLM 타이브레이크로 연기 해제 가능
            defer, reason = self._resolve_ambiguous_sell(
                "KR", df, code, name,
                this_holding_info.get("qty", 0),
                this_holding_info.get("avg_price", 0),
                float(df["close"].iloc[-1]), defer, reason,
            )
            if defer:
                logger.info("%s SELL 연기: %s", name, reason)
            else:
                logger.info("%s SELL 확정: %s", name, reason)
                # 모호익절 오버라이드면 reason을 넘겨 EOD 패턴(ambiguous_take)으로 측정
                self._handle_sell(code, name,
                                  force_reason=(reason if "모호판정" in (reason or "") else ""))
        elif signal == "sell" and not has_this_stock:
            # SELL + 미보유: 현금 대기 시간에 따라 기준 완화
            idle_hours = self._cash_idle_hours()
            if idle_hours >= 3:
                # 3시간+ 현금 유휴 → SELL이어도 확신도 낮으면 소량 진입
                confidence = 0
                analysis = self.strategy.get_last_analysis()
                if analysis:
                    confidence = analysis.get("confidence", 0)
                if confidence <= 0.40:  # 약한 SELL만 (강한 SELL은 존중)
                    logger.info("%s: SELL(약 %.0f%%) + %d시간 유휴 → 소량 진입", name, confidence * 100, idle_hours)
                    self._handle_buy(code, name, price, df, is_long_term=False, mtf=mtf)
                else:
                    logger.info("%s: SELL(강 %.0f%%) → 현금 대기 유지", name, confidence * 100)
            else:
                logger.info("%s: SELL 미보유 → 현금 대기 (%d시간)", name, idle_hours)
        elif signal == "hold":
            # 핵심(whitelist) 코어 타깃: hold 신호여도 보유가 conviction 하한
            # 미만이면 목표까지 '단번에' 탑업한다(삼성 1주→목표, 하이닉스 0→1주). 기존 분할
            # 매수/pyramid/idle-buy 게이트를 모두 우회 — 삼성/하이닉스가 영영 안 사지던 문제 해결.
            held_qty_wl = (this_holding_info.get("qty", 0)
                           if (has_this_stock and this_holding_info) else 0)
            pr_wl = ((this_holding_info.get("profit_rate", 0) or 0) / 100
                     if (has_this_stock and this_holding_info) else 0.0)
            if self._maybe_core_topup_kr(code, name, price, held_qty_wl, intraday_change,
                                         profit_rate=pr_wl):
                return
            # HOLD + 보유 0: 유휴 시간에 따라 기준 완화
            idle_hours = self._cash_idle_hours()
            # 0~1시간: 확신도 50% 이상, 1~2시간: 30%, 2시간+: 15%
            if idle_hours >= 2:
                min_conf = 0.15
            elif idle_hours >= 1:
                min_conf = 0.30
            else:
                min_conf = 0.50

            try:
                bal = self.client.get_balance()
                if not bal.get("holdings") and bal["cash"] > self.min_amount:
                    confidence = 0
                    analysis = self.strategy.get_last_analysis()
                    if analysis:
                        confidence = analysis.get("confidence", 0)
                    if confidence >= min_conf:
                        logger.info("보유 0 + hold 확신도 %.0f%% ≥ %.0f%% (유휴 %d시간) → 매수",
                                    confidence * 100, min_conf * 100, idle_hours)
                        self._handle_buy(code, name, price, df, is_long_term=False, mtf=mtf)
                    else:
                        logger.info("보유 0 + hold 확신도 %.0f%% < %.0f%% → 대기",
                                    confidence * 100, min_conf * 100)
            except Exception:
                pass

            # 대안 종목 추천
            if self.use_claude:
                analysis = self.strategy.get_last_analysis()
                if analysis:
                    alts = analysis.get("alternative_picks", [])
                    if alts:
                        logger.info("대안 종목: %s", alts)

        # ── 소액 잔여금 소진: 이 종목이든 다른 종목이든 1주라도 매수 ──
        # 최근 60일 실측: 일반 진입 +1.87M원 vs 잔금소진 -446k원. 현금 집행은
        # _cash_deployment_ratio가 고확신 정상 BUY의 크기를 키우는 방식으로 담당한다.
        # 저품질 우회 주문은 명시 opt-in일 때만 유지한다.
        if not (self.config.get("cash_deployment", {}) or {}).get(
                "allow_leftover_orders", True):
            return
        try:
            bal_final = self.client.get_balance()
            remaining = bal_final["cash"]
            logger.info("잔금체크: 현금 %s원, min_amount %s원, price %s원",
                        f"{remaining:,}", f"{self.min_amount:,}", f"{price:,}")
            if remaining < 200_000 and remaining >= min(self.min_amount, 5000):
                # 이 종목 추가 매수 가능하면
                if remaining >= price and price > 0 and signal not in ("sell",):
                    logger.info("잔여금 %s원 → %s 추가 매수", f"{remaining:,}", name)
                    self._handle_buy(code, name, price, df, is_long_term=False, mtf=mtf)
                else:
                    # 이 종목은 비싸거나 SELL → 다른 싼 종목 매수
                    #: 매수 gate(장전 sentiment / defensive) 우회 차단
                    analysis2 = self.strategy.get_last_analysis() if self.use_claude else None
                    conf2 = (analysis2 or {}).get("confidence", 0.5) if analysis2 else 0.5
                    allow_pm2, pm_reason2 = self._pre_market_buy_gate("KR", conf2, symbol=code)
                    if not allow_pm2:
                        logger.info("KR 잔금 소진 차단 (%s): %s", name, pm_reason2)
                        return
                    if getattr(self, "_defensive_mode", False) and conf2 < 0.70:
                        logger.info("KR 잔금 소진 차단 (defensive, %s): 확신도 %.0f%% < 70%%",
                                    name, conf2 * 100)
                        return
                    logger.info("잔여금 %s원, %s(주당%s원) 매수 불가 → 다른 종목 탐색",
                                f"{remaining:,}", name, f"{price:,}")
                    from zusik.analysis.indicators import slow_bleed as _sb, momentum_score as _ms
                    for stock in self.kr_stocks:
                        sc = stock["code"]
                        sn = stock.get("name", sc)
                        if sc == code:
                            continue
                        try:
                            sp = self.client.get_current_price(sc)
                            s_price = sp["price"]
                            if not (s_price > 0 and remaining >= s_price):
                                logger.info("잔금 소진: %s 주당 %s원 > 잔여 %s원, 스킵",
                                            sn, f"{s_price:,}", f"{remaining:,}")
                                continue
                            # 장전 게이트는 실제 매수 종목 기준으로 재평가 — 위 게이트는 원래
                            # 종목(symbol=code) 기준이라 whitelist 우회가 대체 종목으로 새면 안 됨
                            allow_alt, alt_pm = self._pre_market_buy_gate("KR", conf2, symbol=sc)
                            if not allow_alt:
                                logger.info("잔금 소진 %s 스킵: %s", sn, alt_pm)
                                continue
                            # 재진입 차단/일일 매도 한도 — US 잔여 소진과 동일한 churn 가드
                            alt_blocked, alt_br = self._is_reentry_blocked(sc, s_price)
                            if alt_blocked:
                                logger.info("잔금 소진 %s 스킵: %s", sn, alt_br)
                                continue
                            alt_limited, _al = self._is_daily_sell_limit(sc)
                            if alt_limited:
                                logger.info("잔금 소진 %s 스킵: 일일 매도 한도 도달", sn)
                                continue
                            # 모멘텀 가드: 약세/출혈 종목은 잔금 소진 후보에서 제외
                            s_df = self.client.get_ohlcv(sc)
                            if s_df is None or s_df.empty:
                                logger.info("잔금 소진 %s 스킵: OHLCV 없음", sn)
                                continue
                            bleed = _sb(s_df)
                            mom = _ms(s_df)
                            if bleed.get("is_bleeding") or mom <= 0:
                                logger.info("잔금 소진 %s 스킵: 출혈=%s 모멘텀=%+.2f",
                                            sn, bleed.get("is_bleeding"), mom)
                                continue
                            s_qty = remaining // s_price
                            logger.info("잔금 소진: %s %d주 × %s원 매수 (모멘텀 %+.2f)",
                                        sn, s_qty, f"{s_price:,}", mom)
                            result = self._submit_kr_market_buy(sc, s_qty, s_price)
                            if result.get("success"):
                                s_qty = self._submitted_order_qty(result, s_qty)
                                if s_qty <= 0:
                                    logger.warning("잔금 소진 성공 응답에 실제 수량 없음: %s", sn)
                                    break
                                # 포지션 상태 기록 — 없으면 본전보호/트레일링이 이 보유를 모름
                                self.positions.record_buy(sc, sn, s_qty, s_price)
                                self.tracker.record_buy(sc, sn, s_qty, s_price, False, "잔금 소진 매수")
                                logger.info("잔금 소진 매수 성공: %s %d주", sn, s_qty)
                            else:
                                logger.warning("잔금 소진 매수 실패: %s — %s", sn, result.get("message", ""))
                            break
                        except Exception as e:
                            logger.warning("잔금 소진 %s 오류: %s", sn, e)
        except Exception as e:
            logger.warning("잔금 소진 체크 오류: %s", e)

    def _rotate_kr_stock(self, sold_code: str, proceeds: int):
        """매도 직후 — 다른 KR 종목 중 매수할 만한 것 탐색."""
        others = [s for s in self.kr_stocks if s["code"] != sold_code]
        if not others:
            return
        for stock in others:
            c = stock["code"]
            nm = stock.get("name", c)
            try:
                p = self.client.get_current_price(c)
                price = p["price"]
                if price <= 0 or price > proceeds:
                    continue
                df = self.client.get_ohlcv(c)
                if df is None:
                    continue
                self.strategy.set_stock(c, nm)
                sig = self.strategy.analyze(df)
                if sig in ("buy", "long_term_buy"):
                    logger.info("매도→회전: %s %s → 매수 시도", nm, sig)
                    self._handle_buy(c, nm, price, df, is_long_term=(sig == "long_term_buy"))
                    return
            except Exception:
                pass
        logger.info("매도→회전: KR 매수 대상 없음")

    @staticmethod
    def _compute_rs(stock_df, index_df, days: int = 20) -> float:
        """20일 상대강도: 종목 수익률 - 지수 수익률.

        실측: 승자(HPE +955k, BB +171k, NVDA +78k)는 전부 지수 아웃퍼폼 모멘텀.
        단발 신호 매수(52주고가 n=18 -109k, 정배열 n=6 -17k)는 진입 시점에 이미
        식은(지수 언더퍼폼) 종목이 다수 — 선별 후보를 RS로 로컬 검증한다.
        """
        try:
            if stock_df is None or index_df is None:
                return 0.0
            if len(stock_df) < days + 1 or len(index_df) < days + 1:
                return 0.0
            s = float(stock_df["close"].iloc[-1]) / float(stock_df["close"].iloc[-(days + 1)]) - 1.0
            i = float(index_df["close"].iloc[-1]) / float(index_df["close"].iloc[-(days + 1)]) - 1.0
            return s - i
        except Exception:
            return 0.0

    @staticmethod
    def _realized_vol(df) -> float:
        """일별 수익률 표준편차 (최근 20봉). 방어주(저변동) 판별용. 데이터 없으면 0."""
        try:
            r = df["close"].pct_change().dropna().tail(20)
            v = float(r.std())
            return v if v == v else 0.0   # NaN 가드
        except Exception:
            return 0.0

    def _load_active_event_sectors(self) -> set:
        try:
            import json
            if os.path.exists(self._ACTIVE_EVENT_FILE):
                with open(self._ACTIVE_EVENT_FILE, encoding="utf-8") as f:
                    return set(json.load(f).get("sectors", []))
        except Exception:
            pass
        return set()

    def _refresh_active_event_sectors(self, report_text: str = ""):
        """장전 리포트(뉴스)에서 이벤트 감지 → 활성 수혜 섹터 갱신 + 영속화.

        이벤트 로테이션: _rank_by_relative_strength 가 활성 섹터 종목을 부스트.
        평시·이벤트 없음이면 빈 집합(부스트 없음).
        """
        try:
            res = self.signals.check_event_beneficiary(report_text or "", self._market_condition)
        except Exception:
            res = None
        sectors = set(res.get("sectors", [])) if res else set()
        self._active_event_sectors = sectors
        try:
            import json
            os.makedirs("data", exist_ok=True)
            with open(self._ACTIVE_EVENT_FILE, "w", encoding="utf-8") as f:
                json.dump({"sectors": sorted(sectors),
                           "events": (res or {}).get("event_labels", []),
                           "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        if sectors:
            logger.info("활성 이벤트 섹터: %s (%s)", ", ".join(sorted(sectors)),
                        ", ".join((res or {}).get("event_labels", [])))

    def _rank_by_relative_strength(self, stocks: list[dict], market: str) -> list[dict]:
        """선별 후보를 지수 대비 RS로 필터 + 상황 적응(레짐/로테이션) 틸트로 정렬.

        RS < 임계는 제외(식은 모멘텀). 통과분 정렬은 RS 에 틸트를 더해:
          - 레짐 적응: 하락장(bear score↑)이면 고변동 종목에 페널티(방어=저변동 선호),
            상승장이면 RS(모멘텀) 그대로. (config.selection.regime_adaptive)
          - 로테이션: 최근 매도(_reentry_block) 종목은 소폭 디프리오리타이즈 — 같은 테마 쏠림 방지.
        인버스/파생은 RS·틸트 제외(태생적 역RS). 실패 시 원본 그대로(보조 게이트).
        """
        if not stocks:
            return stocks
        sel = (self.config.get("selection", {}) or {})
        try:
            if market == "KR":
                index_df = self.client.get_ohlcv("069500", period=self.period)
            else:
                index_df = self.client.get_us_ohlcv("QQQ", "NASD")
            if index_df is None or index_df.empty:
                return stocks

            try:
                bear = float(self._bearish_regime_score())
            except Exception:
                bear = 0.0
            recently_sold = set((getattr(self, "_reentry_block", {}) or {}).keys())
            regime_on = bool(sel.get("regime_adaptive", True))
            bear_gate = float(sel.get("regime_bear_gate", 0.40))
            min_rs = float(sel.get("min_relative_strength", self._RS_DROP_THRESHOLD))
            vol_w = float(sel.get("regime_vol_weight", 1.5))
            rot_on = bool(sel.get("rotation", True))
            rot_pen = float(sel.get("rotation_penalty", 0.03))
            event_sectors = getattr(self, "_active_event_sectors", set()) or set()
            event_boost = float(sel.get("event_boost", 0.05))
            defensive = regime_on and bear >= bear_gate

            scored: list[tuple[float, dict]] = []
            exempt: list[dict] = []
            for s in stocks:
                sym = s.get("code", "") or s.get("ticker", "")
                if self._is_inverse(sym) or self._is_derivative_etf(sym, s.get("name", "")):
                    exempt.append(s)  # 헷지 상품은 RS 게이트 미적용, 리스트 유지
                    continue
                try:
                    if market == "KR":
                        df = self.client.get_ohlcv(sym, period=self.period)
                    else:
                        df = self.client.get_us_ohlcv(sym, s.get("exchange", "NASD"))
                except Exception:
                    df = None
                rs = self._compute_rs(df, index_df)
                if rs < min_rs:
                    logger.info("RS 게이트 제외 (%s %s): 지수 대비 %+.1f%%p — 식은 모멘텀",
                                market, s.get("name", sym), rs * 100)
                    continue
                score = rs
                if defensive:
                    # 하락장: 고변동 종목 페널티 → 저변동(방어) 종목 우선
                    score -= vol_w * bear * max(0.0, self._realized_vol(df) - 0.02)
                if rot_on and sym in recently_sold:
                    score -= rot_pen   # 최근 매도 → 로테이션(쏠림 방지)
                if event_sectors and event_boost and (
                        self.signals.sectors_of(sym, s.get("name", "")) & event_sectors):
                    score += event_boost   # 활성 이벤트 수혜 섹터 → 부스트(이벤트 로테이션)
                ranked_stock = dict(s)
                ranked_stock["_relative_strength"] = rs
                # 자동 스크리너 점수와 RS를 함께 반영하되, 서로 다른 척도가 목록을
                # 뒤집지 않도록 RS를 주축으로 작은 보너스만 준다.
                screen_score = float(ranked_stock.get("screen_score", 0.0) or 0.0)
                score += max(-0.02, min(0.02, screen_score * 0.02))
                ranked_stock["_selection_score"] = score
                scored.append((score, ranked_stock))
            scored.sort(key=lambda x: -x[0])
            ranked = [s for _, s in scored] + exempt
            if scored:
                logger.info("RS 랭킹 [%s]: 1위 %s, %d/%d 통과 (bear %.2f → %s 틸트)",
                            market, scored[0][1].get("name", ""), len(ranked), len(stocks),
                            bear, "방어(저변동)" if defensive else "모멘텀")
                return ranked
            if exempt:
                return exempt
            # 전부 약하면 현금 유지. 원본 복원은 RS 게이트를 무효화해 지수
            # 언더퍼폼 종목만 다시 매수 후보로 살리는 fail-open이었다.
            logger.warning("RS 랭킹 [%s]: %d종 전부 기준(%+.1f%%p) 미달 — 후보 없음",
                           market, len(stocks), min_rs * 100)
            return []
        except Exception as e:
            logger.warning("RS 랭킹 실패 (%s): %s — 원본 유지", market, e)
            return stocks

    def _is_derivative_etf(self, code: str = "", name: str = "") -> bool:
        """파생ETF 여부 — 인버스 맵 등록 + 이름 키워드 (선물/레버리지/커버드콜 포함)."""
        if code and self._is_inverse(code):
            return True
        if name:
            n = name.lower()
            return any(kw in n for kw in self._DERIVATIVE_NAME_KEYWORDS)
        return False

    def _filter_derivatives(self, stocks: list[dict], market: str = "KR") -> list[dict]:
        """파생ETF 미신청 계좌(broker.derivative_etf_enabled=false)면 파생ETF 제거."""
        if getattr(self, "derivative_etf_enabled", True):
            return list(stocks)
        out: list[dict] = []
        removed: list[str] = []
        for s in stocks:
            code = s.get("code", "") or s.get("ticker", "")
            name = s.get("name", "")
            if self._is_derivative_etf(code, name):
                removed.append(f"{name}({code})")
            else:
                out.append(s)
        if removed:
            logger.info("%s 파생ETF 제외 (%d종): %s",
                        market, len(removed), ", ".join(removed))
        return out

    def _should_force_rotate_to_index(self) -> tuple[bool, dict]:
        """상승장 + 인덱스 노출 부족 → 회전 권고.

        조건:
          1. config.index_follow.enabled
          2. peace
          3. bull score ≥ trigger
          4. 쿨다운 만료 (사이클당 과도한 회전 방지)
          5. KR 또는 US 인덱스 비중 < min
        """
        cfg = self.config.get("index_follow", {})
        if not cfg.get("enabled", False):
            return False, {}
        if getattr(self, "_market_condition", "peace") != "peace":
            return False, {"skip_reason": f"market={self._market_condition}"}

        bull = self._bullish_regime_score()
        trigger = float(cfg.get("bull_score_trigger", 0.50))
        if bull < trigger:
            return False, {"bull": bull, "trigger": trigger, "skip_reason": "bull below trigger"}

        import time as _time
        cooldown_h = float(cfg.get("rotation_cooldown_hours", 12))
        last_rot = getattr(self, "_last_index_rotation", 0.0)
        if _time.time() - last_rot < cooldown_h * 3600:
            return False, {"skip_reason": "cooldown"}

        alloc = self._index_allocation_ratios()
        min_kr = float(cfg.get("min_kr_allocation", 0.30))
        min_us = float(cfg.get("min_us_allocation", 0.30))
        plan: dict = {"bull": bull, "alloc": alloc, "min_kr": min_kr, "min_us": min_us}

        # 의미 있는 자금이 있는 시장만 (소액 dust 회전 방지)
        if alloc["kr"] < min_kr and alloc["kr_total"] >= 20_000:
            plan["kr_rotate"] = True
        if alloc["us"] < min_us and alloc["us_total_usd"] >= 20:
            plan["us_rotate"] = True

        if plan.get("kr_rotate") or plan.get("us_rotate"):
            return True, plan
        return False, plan

    def _maybe_rotate_to_index(self) -> None:
        """run_once 시작에 호출. 사이클당 1회 인덱스 회전 평가.

        구현 방침:
          - 가용 현금 ≥ 10% 면 인덱스 ETF 직접 매수
          - 현금 부족 시 가장 약한 비인덱스/비인버스 보유종목 1개 매도 (다음 사이클에 매수)
          - 사이클당 KR/US 각 1개 종목까지만 회전 (점진적)
        """
        try:
            should_rotate, plan = self._should_force_rotate_to_index()
            if not should_rotate:
                return
        except Exception:
            logger.debug("인덱스 회전 평가 실패", exc_info=True)
            return

        rotated_any = False

        if plan.get("kr_rotate"):
            try:
                rotated_any |= self._rotate_one_kr_to_index(plan)
            except Exception:
                logger.warning("KR 인덱스 회전 실패", exc_info=True)

        if plan.get("us_rotate"):
            try:
                rotated_any |= self._rotate_one_us_to_index(plan)
            except Exception:
                logger.warning("US 인덱스 회전 실패", exc_info=True)

        if rotated_any:
            import time as _time
            self._last_index_rotation = _time.time()

    def _rotate_one_kr_to_index(self, plan: dict) -> bool:
        """KR 인덱스 회전 1단계 (현금 충분→매수, 부족→약세 종목 매도)."""
        if not self.client.is_market_open():
            return False
        kr_bal = self.client.get_balance()
        cash = int(kr_bal.get("cash", 0) or 0)
        kr_total = max(cash + int(kr_bal.get("total_eval", 0) or 0), 1)
        cash_ratio = cash / kr_total
        target_code = "069500"
        target_name = self._INDEX_ETF_KR[target_code]

        if cash_ratio >= 0.10 and cash >= max(self.min_amount, 5000):
            # 현금 충분 → 인덱스 매수
            try:
                price_info = self.client.get_current_price(target_code)
                price = int(price_info.get("price", 0) or 0)
                if price <= 0:
                    return False
                df = None
                try:
                    df = self.client.get_ohlcv(target_code)
                except Exception:
                    pass
                logger.info("인덱스 회전(KR): bull=%.2f, alloc=%.0f%% < %.0f%% → %s 매수",
                            plan["bull"], plan["alloc"]["kr"] * 100, plan["min_kr"] * 100, target_name)
                if self.discord:
                    try:
                        self.discord.notify_info(
                            f"상승장 회전 (bull {plan['bull']:.2f}) — "
                            f"KOSPI 노출 {plan['alloc']['kr']*100:.0f}% < {plan['min_kr']*100:.0f}% "
                            f"→ {target_name} 매수 시도"
                        )
                    except Exception:
                        pass
                self._handle_buy(target_code, target_name, price, df=df)
                return True
            except Exception:
                logger.debug("인덱스 매수 실패", exc_info=True)
                return False

        # 현금 부족 → 가장 약한 비인덱스/비인버스 종목 1개 매도
        from zusik.analysis.indicators import momentum_score as _mom
        weakest = None
        weakest_score = 999.0
        for h in kr_bal.get("holdings", []) or []:
            code = h.get("code")
            qty = int(h.get("qty", 0) or 0)
            if not code or qty <= 0:
                continue
            if code in self._INDEX_ETF_KR or self._is_inverse(code):
                continue
            try:
                df_h = self.client.get_ohlcv(code)
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

        code = weakest["code"]
        name = weakest.get("name", code)
        qty = int(weakest.get("qty", 0) or 0)
        cur_price = int(weakest.get("current_price", 0) or 0)
        avg_price = int(weakest.get("avg_price", 0) or 0)
        logger.info("인덱스 회전(KR): bull=%.2f, %s(%s) 약세 mom=%.2f → 매도 (다음 사이클 인덱스 매수)",
                    plan["bull"], name, code, weakest_score)
        if self.discord:
            try:
                self.discord.notify_info(
                    f"상승장 회전: {name}({code}) 약세 (mom {weakest_score:+.2f}) 매도 "
                    f"→ 현금 확보 후 다음 사이클에 KOSPI 인덱스 매수"
                )
            except Exception:
                pass
        result = self.client.sell_market(code, qty)
        if result.get("success"):
            pnl_info = {"realized_pnl": 0, "realized_rate": 0}
            try:
                pnl_info = self.tracker.record_sell(
                    code, name, qty, cur_price, avg_price,
                    reason="상승장 회전 — 인덱스 노출 확대 위해 약세 종목 청산",
                ) or pnl_info
            except Exception:
                logger.debug("회전 매도 기록 실패", exc_info=True)
            try:
                self.positions.record_sell(code, qty)
            except Exception:
                pass
            if self.discord:
                try:
                    self.discord.notify_trade(
                        side="sell", stock_name=name, stock_code=code,
                        qty=qty, price=cur_price,
                        reason="상승장 회전 (KR 인덱스 노출 확대)",
                        realized_pnl=pnl_info.get("realized_pnl", 0),
                        realized_rate=pnl_info.get("realized_rate", 0),
                    )
                except Exception:
                    logger.debug("회전 매도 Discord 알림 실패", exc_info=True)
            return True
        return False

    def _run_auto_screening(self):
        """후보 풀 100+종 주기 스크리닝 — 로컬 가격 데이터로 재랭킹.

        설정된 시간 간격마다 호출:
          1. KR/US 후보 풀 OHLCV 병렬 fetch
          2. 설정된 방법(momentum/trend/low_vol/monte_carlo)으로 점수 계산
          3. 상위 N과 whitelist를 후보로 구성
          4. 동일한 상대강도 게이트를 거쳐 실제 감시 목록 갱신 + 디스코드 알림
        """
        self._apply_pending_screened_if_unlocked()
        if self._screening_authority() not in ("auto", "hybrid"):
            return

        #: 일 1회 → 시간 간격(auto_screen_interval_hours, 기본 4h)으로.
        # 감시종목이 하루종일 고정돼 시장 변동을 반영 못 하던 문제 — 이제 N시간마다 현재
        # MC/모멘텀으로 후보 풀을 재랭킹해 watch list가 시장 흐름을 따라간다.
        import time as _t_scr
        now_ts = _t_scr.time()
        interval_h = float((self.config.get("screening", {}) or {}).get("auto_screen_interval_hours", 4))
        last_ts = getattr(self, "_last_auto_screen_ts", 0.0)
        if (now_ts - last_ts) < interval_h * 3600:
            return
        self._last_auto_screen_ts = now_ts

        import threading

        def _run():
            try:
                from zusik.analysis.auto_screener import (AutoScreener, KR_CANDIDATE_POOL,
                                            US_CANDIDATE_POOL)
                runner = None
                n_paths = 2000
                sel_method = str((self.config.get("screening", {}) or {}).get(
                    "method", "momentum"
                )).strip().lower()
                if sel_method == "monte_carlo":
                    logger.info("자동 스크리닝: numpy MC %d path", n_paths)
                else:
                    logger.info("자동 스크리닝: %s", sel_method)

                screener = AutoScreener(kr_pick=5, us_pick=5)

                def _fetch_kr(code, name):
                    try:
                        #: 선별 MC는 30봉→100봉이면 충분(hist=last 60 returns).
                        # 250봉(3콜×271)은 220s로 240s 타임아웃에 위험 → 100봉(2콜)으로 경량화.
                        # 250봉 깊은 데이터는 모델선택 글로벌 백테스트(25종목)에서만 사용.
                        return self.client.get_daily_long(code, days=100)
                    except Exception:
                        return None

                def _fetch_us(ticker, name, exchange):
                    try:
                        #: 585종목 × 250봉(3콜)은 screen_market 120s 타임아웃
                        # 초과(81/585 미완) → 스크리닝 전체 실패. 선별은 100봉(1콜)이면 MC 충분.
                        # 250봉 깊은 데이터는 모델선택 글로벌 백테스트(25종목)에서만 사용.
                        return self.client.get_us_ohlcv(ticker, exchange, count=100, period=self.period)
                    except Exception:
                        return None

                logger.info("자동 스크리닝 시작 (KR %d / US %d 후보)",
                            len(KR_CANDIDATE_POOL), len(US_CANDIDATE_POOL))

                import time as _t
                t0 = _t.time()
                kr_scored = screener.screen_market(KR_CANDIDATE_POOL, _fetch_kr,
                                                   runner, n_paths=n_paths, method=sel_method)
                us_scored = screener.screen_market(US_CANDIDATE_POOL, _fetch_us,
                                                   runner, n_paths=n_paths, method=sel_method)
                elapsed = _t.time() - t0
                logger.info("자동 스크리닝 완료: %.1f초 (KR %d / US %d 평가)",
                            elapsed, len(kr_scored), len(us_scored))

                # 가격 캡 — 소액 계좌가 살 수 없는 종목은 추천에서 제외.
                # Claude 선별과 동일 공식: (cash + total_eval) // 2
                max_kr_price = 0
                max_us_price = 0.0
                try:
                    bal = self.client.get_balance()
                    kr_cash = bal.get("cash", 0)
                    kr_total = kr_cash + bal.get("total_eval", 0)
                    max_kr_price = max(kr_cash, kr_total // 2)
                except Exception:
                    pass
                try:
                    us_bal = self.client.get_us_balance()
                    us_cash = float(us_bal.get("cash_usd", 0) or 0)
                    us_eval = sum(
                        float(h.get("qty", 0) or 0) * float(h.get("current_price", 0) or 0)
                        for h in us_bal.get("holdings", [])
                    )
                    max_us_price = max(us_cash, (us_cash + us_eval) / 2)
                except Exception:
                    pass
                if max_kr_price > 0 or max_us_price > 0:
                    logger.info("자동 스크리닝 가격 캡: KR %s원 / US $%.0f",
                                f"{max_kr_price:,}", max_us_price)

                # config의 screening 카운트 + 단일주 슬롯 강제
                screen_cfg = self.config.get("screening", {})
                kr_pick = screen_cfg.get("kr_count", 5)
                us_pick = screen_cfg.get("us_count", 5)
                kr_min_single = screen_cfg.get("min_single_stocks_kr", 0)
                us_min_single = screen_cfg.get("min_single_stocks_us", 0)
                kr_top = screener.filter_top(kr_scored, kr_pick,
                                              max_price=max_kr_price,
                                              min_single_stocks=kr_min_single)
                us_top = screener.filter_top(us_scored, us_pick,
                                              max_price=max_us_price,
                                              min_single_stocks=us_min_single)

                def _screen_metric(row: dict) -> str:
                    mc = row.get("mc")
                    if mc:
                        return (f"score={row['score']:.3f} "
                                f"P={mc['p_profit']*100:.0f}% "
                                f"VaR95={mc['var95']*100:+.1f}%")
                    return f"{row.get('method', sel_method)} score={row['score']:.3f}"

                # 결과 로그
                logger.info("─ KR 상위 후보 ─")
                for r in kr_top:
                    logger.info("  %s %s: %s", r["info"][0], r["info"][1],
                                _screen_metric(r))
                logger.info("─ US 상위 후보 ─")
                for r in us_top:
                    logger.info("  %s %s: %s", r["info"][0], r["info"][1],
                                _screen_metric(r))

                # watch list 후보 구성 — whitelist도 강제 통과가 아닌 '후보'로만 추가한다.
                # 최종 적용은 _apply_screened_stocks에서 가격/파생/상대강도 게이트를 공통 적용.
                screened_result = {}
                if kr_top:
                    kr_list = [{"code": r["info"][0], "name": r["info"][1],
                                "screen_score": float(r.get("score", 0.0))} for r in kr_top]
                    kr_list = self._filter_derivatives(kr_list, market="KR")
                    # blacklist 강제 제외 (사용자 구매 금지 종목)
                    bl_kr = set(screen_cfg.get("blacklist_kr", []) or [])
                    if bl_kr:
                        before = len(kr_list)
                        kr_list = [s for s in kr_list if s["code"] not in bl_kr]
                        if len(kr_list) < before:
                            logger.info("KR blacklist 제외: %s", ", ".join(bl_kr))
                    # whitelist 후보 편입 — 중복 제거·blacklist 우선. RS 미달이면 최종 제외.
                    wl = screen_cfg.get("whitelist_kr", []) or []
                    existing = {s["code"] for s in kr_list}
                    for w in wl:
                        code = w.get("code", "")
                        if code and code not in existing and code not in bl_kr:
                            kr_list.append({"code": code, "name": w.get("name", code)})
                            existing.add(code)
                    if wl:
                        logger.info("KR whitelist 후보 편입 (%d종, RS 게이트 적용): %s",
                                    len(wl), ", ".join(w.get("name", w.get("code","")) for w in wl))
                    screened_result["kr"] = kr_list
                if us_top:
                    us_list = [{"ticker": r["info"][0], "name": r["info"][1],
                                "exchange": r["info"][2],
                                "screen_score": float(r.get("score", 0.0))} for r in us_top]
                    us_list = self._filter_derivatives(us_list, market="US")
                    wl_us = screen_cfg.get("whitelist_us", []) or []
                    existing_us = {s["ticker"] for s in us_list}
                    for w in wl_us:
                        tk = w.get("ticker", "")
                        if tk and tk not in existing_us:
                            us_list.append({"ticker": tk, "name": w.get("name", tk),
                                            "exchange": w.get("exchange", "NASD")})
                            existing_us.add(tk)
                    if wl_us:
                        logger.info("US whitelist 후보 편입 (%d종, RS 게이트 적용): %s",
                                    len(wl_us), ", ".join(w.get("name", w.get("ticker","")) for w in wl_us))
                    screened_result["us"] = us_list

                if screened_result:
                    self._apply_screened_stocks(screened_result, tag="자동 스크리닝")

                # Discord 알림
                if self.discord and (kr_top or us_top):
                    try:
                        msg_lines = [f"자동 스크리닝 결과 ({sel_method})"]
                        if kr_top:
                            msg_lines.append("**KR 상위:**")
                            for r in kr_top[:5]:
                                msg_lines.append(
                                    f"  {r['info'][1]} ({r['info'][0]}): "
                                    f"{_screen_metric(r)}")
                        if us_top:
                            msg_lines.append("**US 상위:**")
                            for r in us_top[:5]:
                                msg_lines.append(
                                    f"  {r['info'][1]} ({r['info'][0]}): "
                                    f"{_screen_metric(r)}")
                        self.discord.notify_info("\n".join(msg_lines))
                    except Exception:
                        pass
            except Exception:
                logger.exception("자동 스크리닝 오류")

        threading.Thread(target=_run, daemon=True).start()
