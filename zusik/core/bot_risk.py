from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime


from zusik.storage.portfolio_tracker import PortfolioTracker


logger = logging.getLogger(__name__)


class RiskExitMixin:
    """RiskExitMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _load_reentry_block(self):
        import time
        try:
            if os.path.exists(self._REENTRY_BLOCK_FILE):
                with open(self._REENTRY_BLOCK_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                parsed = {}
                for k, v in data.items():
                    if not isinstance(v, list) or len(v) < 2 or float(v[0]) <= now:
                        continue
                    sell_px = float(v[2]) if len(v) >= 3 else 0.0  # 레거시 2-튜플 호환
                    parsed[k] = (float(v[0]), str(v[1]), sell_px)
                self._reentry_block = parsed
        except Exception as e:
            logger.warning("재진입 차단 파일 로드 실패: %s", e)
            self._reentry_block = {}

    def _save_reentry_block(self):
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._REENTRY_BLOCK_FILE, "w", encoding="utf-8") as f:
                json.dump({k: [v[0], v[1], (v[2] if len(v) >= 3 else 0.0)]
                           for k, v in self._reentry_block.items()},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("재진입 차단 파일 저장 실패: %s", e)

    def _register_reentry_block(self, code: str, reason: str, sell_price: float = 0.0):
        """매도 reason 기반으로 재진입 차단 시간 결정.

        - 급락/출혈/트레일링/손절 → 24시간 (crash 태그, 돌파 override 없음)
        - 본전보호/익절/종목교체 등 익절 매도 → 12h 세션 차단 (session 태그, +2% 돌파 override)
          = 현 세션 평평 재매수(-214k churn) 차단, 다음 세션·돌파 재진입은 허용.
        """
        import time
        r_lower = reason.lower()
        long_block = (
            "급락" in reason or "출혈" in reason or "트레일" in reason
            or "손절" in reason or "강제" in reason
            or any(k in r_lower for k in ("crash", "bleed", "trail", "slow", "stop"))
        )
        if long_block:
            hours, tag = 24.0, "crash"
        else:
            hours, tag = self._SESSION_BLOCK_HOURS, "session"
        until = time.time() + hours * 3600
        self._reentry_block[code] = (until, tag, float(sell_price or 0.0))
        self._save_reentry_block()
        logger.info("재진입 차단 등록: %s (%s %.0fh, 매도가 %s, 사유=%s)",
                    code, tag, hours, f"{sell_price:,.2f}" if sell_price else "-", reason[:40])

    def _is_reentry_blocked(self, code: str, price: float = 0.0) -> tuple[bool, str]:
        import time
        entry = self._reentry_block.get(code)
        if not entry:
            return False, ""
        until = float(entry[0]); tag = str(entry[1])          # 레거시 2-튜플도 안전
        sell_px = float(entry[2]) if len(entry) > 2 else 0.0
        now = time.time()
        if now >= until:
            del self._reentry_block[code]
            self._save_reentry_block()
            return False, ""
        # +2% 돌파 override (session 차단 한정): 매도가 위로 재돌파 = 추세 지속 재진입 → 허용
        if (tag == "session" and price > 0 and sell_px > 0
                and price >= sell_px * self._REENTRY_BREAKOUT):
            return False, ""
        remain_min = (until - now) / 60
        return True, f"재진입 차단 ({remain_min:.0f}분 남음, {tag})"

    def _record_sell_for_churn_guard(self, code: str, reason: str, sell_price: float = 0.0):
        """매도 직후 호출: 일일 횟수 증가 + 재진입 차단 등록 + 칼날 가드 등록."""
        self._daily_sell_count[code] = self._daily_sell_count.get(code, 0) + 1
        self._register_reentry_block(code, reason, sell_price)
        self._register_knife_block(code, reason, sell_price)

    def _load_knife_block(self):
        try:
            if os.path.exists(self._KNIFE_BLOCK_FILE):
                with open(self._KNIFE_BLOCK_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                self._knife_block = {
                    k: (float(v[0]), float(v[1])) for k, v in raw.items()
                }
        except Exception as e:
            logger.warning("칼날 차단 파일 로드 실패: %s", e)
            self._knife_block = {}

    def _save_knife_block(self):
        try:
            with open(self._KNIFE_BLOCK_FILE, "w", encoding="utf-8") as f:
                json.dump({k: [v[0], v[1]] for k, v in self._knife_block.items()},
                          f, ensure_ascii=False)
        except Exception as e:
            logger.warning("칼날 차단 파일 저장 실패: %s", e)

    def _register_knife_block(self, code: str, reason: str, sell_price: float):
        """익절 매도일 때만 등록 — 손절 계열은 _register_reentry_block 24h가 별도 처리."""
        if not hasattr(self, "_knife_block"):  # __new__ 기반 테스트 하네스 방어
            self._knife_block = {}
        if not sell_price or sell_price <= 0:
            return
        r = reason or ""
        profit_exit = ("과매수" in r or "익절" in r or "본전" in r
                       or "overbought" in r.lower() or "surge" in r.lower())
        if not profit_exit:
            return
        import time
        self._knife_block[code] = (time.time() + self._KNIFE_HOURS * 3600, float(sell_price))
        self._save_knife_block()
        logger.info("칼날 가드 등록: %s 매도가 %s 기준 -5%%↓ 재매수 %.0fh 차단",
                    code, f"{sell_price:,.2f}", self._KNIFE_HOURS)

    def _is_knife_reentry(self, code: str, price: float) -> tuple[bool, str]:
        """익절 후 48h 내, 매도가 대비 -5% 넘게 떨어진 재매수인가 (떨어지는 칼날)."""
        entry = getattr(self, "_knife_block", {}).get(code)  # __new__ 테스트 하네스 방어
        if not entry or not price or price <= 0:
            return False, ""
        import time
        until, sell_price = entry
        if time.time() >= until:
            del self._knife_block[code]
            self._save_knife_block()
            return False, ""
        if price < sell_price * self._KNIFE_RETRACE:
            retrace = (price - sell_price) / sell_price * 100
            return True, (f"칼날 재진입 차단 (익절가 {sell_price:,.2f} 대비 {retrace:+.1f}% — "
                          f"블로우오프 후 mean reversion 추격 금지)")
        return False, ""

    def _is_daily_sell_limit(self, code: str) -> tuple[bool, str]:
        n = self._daily_sell_count.get(code, 0)
        if n >= self.DAILY_SELL_LIMIT:
            return True, f"일일 매매 한도 ({n}회 매도 ≥ {self.DAILY_SELL_LIMIT}, churn 방지)"
        return False, ""

    def _is_recently_bought(self, code: str, minutes: int = 30) -> bool:
        """직전 매수 시점이 N분 이내인가. RIOT 4/29 churn(매수 1분 후 매도) 방지용.

        positions._positions[code]['last_buy_date']를 datetime으로 비교.
        """
        try:
            pos = self.positions._positions.get(code) if hasattr(self.positions, "_positions") else None
            if not pos:
                return False
            last_buy_str = pos.get("last_buy_date")
            if not last_buy_str:
                return False
            from datetime import datetime as _dt
            last_buy = _dt.fromisoformat(last_buy_str.replace("Z", "+00:00")) if "T" in last_buy_str else None
            if not last_buy:
                return False
            now = _dt.now() if last_buy.tzinfo is None else _dt.now(last_buy.tzinfo)
            elapsed_min = (now - last_buy).total_seconds() / 60
            return 0 <= elapsed_min < minutes
        except Exception:
            return False

    def _churn_guard(self, code: str, name: str, df=None, price: float = 0.0,
                     is_add_on: bool = False) -> bool:
        """매수 직전 호출. 차단 시 True 반환.

        is_add_on=True (보유 승자 피라미딩 = 추가매수)이면 churn 장벽(재진입/일일매도/칼날)을
        면제한다. 이들은 '다 팔고 되사기(flat 재매수)'의 이중 수수료 churn을 막는
        장치인데, 들고 더 사는 추가매수에는 부적용 — 이중 수수료가 아니고 통제는 plan_add_on의
        수익·레벨 캡이 전담. 품질 게이트(일중급락/약세추세/MC)는 add_on도 그대로 적용.
        라이브 실증: 삼성전자 피크 +8.8%인데 pyramid L0 — 직전 트림 후 재진입블록에 막혀
        승자 증폭이 차단되던 버그.
        """
        if not is_add_on:
            blocked, br = self._is_reentry_blocked(code, price)
            if blocked:
                logger.info("매수 차단 (%s): %s", name, br)
                return True
            exceeded, ex = self._is_daily_sell_limit(code)
            if exceeded:
                logger.info("매수 차단 (%s): %s", name, ex)
                return True
            # 칼날 재진입: 익절 후 48h 내 매도가 -5%↓ 재매수 금지 (HPE -187k형)
            knife, kr_reason = self._is_knife_reentry(code, price)
            if knife and not self._is_inverse(code):
                logger.info("매수 차단 (%s): %s", name, kr_reason)
                return True
        # 일중 -2% 이상 하락한 종목 매수 금지 — prev_close 기준 crash_instant
        # 직전 위험. 4/29 RIOT churn 후 -3%였던 임계를 -2%로 강화 (1단계 fix)
        #: 핵심(whitelist) 종목은 예외 — 딥에서 물타기(평단 하락) 허용해
        # US HPE식 누적(13분할) 패턴을 KR에 이식. 누적량은 _whitelist_min_invest의
        # averaging_down cap + 재진입블록 + 일일매도한도가 별도로 제한.
        intraday = self._last_intraday_change.get(code, 0.0)
        if intraday <= -0.02 and not self._is_inverse(code) and not self._core_hold_through(code):
            logger.info("매수 차단 (%s): 일중 %.1f%% 하락 — crash_instant 직전 위험",
                        name, intraday * 100)
            return True
        # 유동성 게이트: 20일 평균 거래대금이 하한 미만이면 매수 차단 (KR 일반주만).
        # 실측: 미창석유(평균 4.4억, 최소 0.9억) — LLM이 "거래량 20일 평균의 17%로 얇다"
        # 경고문을 쓰면서도 만장일치 보너스로 확신도 94%가 돼 매수됨. 저유동성은 슬리피지·
        # 갭·탈출불가 위험이라 LLM 판단 이전에 로컬에서 끊는다. 인버스/whitelist 면제,
        # 거래량 데이터 없으면 통과(데이터 결함으로 매매 정지 방지). US는 스크리너가
        # 대형주 위주라 보류 — 필요 시 USD 임계로 확장.
        if (df is not None and "volume" in df and len(df) >= 21
                and code.isdigit() and not self._is_inverse(code)
                and not self._is_whitelist(code)):
            try:
                min_turnover = float(self.config.get("risk", {}).get(
                    "min_avg_turnover_krw", 1_000_000_000))
                avg_turnover = float((df["close"] * df["volume"]).iloc[-21:-1].mean())
                if 0 < avg_turnover < min_turnover:
                    logger.info("매수 차단 (%s): 20일 평균 거래대금 %.1f억 < 하한 %.0f억 (저유동성)",
                                name, avg_turnover / 1e8, min_turnover / 1e8)
                    return True
            except Exception:
                pass

        # 추세 필터: 데드크로스 + 60일선 아래면 매수 차단 (인버스 우회)
        if df is not None and not self._is_inverse(code):
            weak, wreason = self._is_weak_trend(df)
            if weak:
                logger.info("매수 차단 (%s): %s", name, wreason)
                return True
        # Monte Carlo 통계 게이트 (Vortex 8x 가속, sub-100ms)
        if df is not None:
            ok, mc_reason = self._mc_buy_gate(code, name, df)
            if not ok:
                logger.info("매수 차단 (%s): %s", name, mc_reason)
                return True
        return False

    def _verify_tick_pnl(self):
        """매 사이클(1분 tick) 손익/자산 무결성 점검 — 로컬·무예외·API 0.

        위반 시 같은 위반셋은 1회만 경고(dedup) + 메신저 알림. 기본은 경고만(매매를 막지
        않음); config.risk.halt_buys_on_integrity_violation=true면 신규매수만 일시 보수화.
        매도/안전망은 절대 막지 않는다(자본보호 우선). 회귀 가드: PnlIntegrityTests."""
        try:
            from zusik.core.resilience import verify_pnl_invariants
            from zusik.storage.portfolio_tracker import EQUITY_CURVE_FILE, _load_json
            curve = _load_json(EQUITY_CURVE_FILE)
            latest = (max(curve, key=lambda c: c.get("date", ""), default=None)
                      if isinstance(curve, list) and curve else None)
            risk_cfg = self.config.get("risk", {}) if isinstance(self.config, dict) else {}
            issues = verify_pnl_invariants(
                trades=getattr(self.tracker, "_trades", []) or [],
                deposits=self.tracker.get_total_deposits(),
                latest_snapshot=latest,
                positions=getattr(self.positions, "_positions", {}) or {},
                tol=float(risk_cfg.get("integrity_tolerance", 0.05)),
            )
            if not issues:
                self._last_integrity_sig = None
                return []
            sig = "|".join(sorted(issues))
            if sig != getattr(self, "_last_integrity_sig", None):
                self._last_integrity_sig = sig
                logger.error("손익 무결성 위반 %d건: %s", len(issues), " / ".join(issues[:5]))
                if self.discord:
                    try:
                        self.discord.notify_error("손익 무결성 경고 — " + " / ".join(issues[:5]))
                    except Exception:
                        pass
            if risk_cfg.get("halt_buys_on_integrity_violation"):
                self._defensive_mode = True
            return issues
        except Exception:
            logger.debug("tick 무결성 점검 예외", exc_info=True)
            return []

    def _check_risks_before_trading(self, market: str = "") -> bool:
        """매매 전: 시장 감지 + 자산 평가 + 모드 자동 전환. False면 매매 중단.

        market="KR"|"US"면 일일 손실한도를 해당 시장 실현손익만으로 판정 —
        US 새벽 손실이 같은 날짜의 KR장 전체를 막던 문제 방지. 빈 값(암호화폐 등)은
        기존대로 전체 합산 기준."""
        from zusik.core.trading_mode import check_mode_change, check_deposit, detect_market_condition

        # 1) 긴급 홀딩 체크 (shelter 모드 아니라 완전 중단)
        if self.risk.is_emergency_hold() and self._active_mode == "shelter":
            logger.warning("대피 모드(shelter) — 매매 중단")
            return False

        # 1-b) 매 사이클 손익/자산 무결성 점검 (로컬·무예외, API 0). 경고만 — 매매는 안 막음.
        self._verify_tick_pnl()

        today = datetime.now().strftime("%Y-%m-%d")

        try:
            balance = self.client.get_balance()
            total_asset = balance["cash"] + balance["total_eval"]
        except Exception:
            total_asset = 0

        # ── 적립금 입금 감지 ──
        if total_asset > 0 and self._prev_cash > 0:
            deposit = check_deposit(self._prev_cash, balance["cash"], total_asset)
            if deposit and self.discord:
                self.discord.notify_error(
                    f"적립금 입금 감지: +{deposit['amount']:,}원 → 총자산 {total_asset:,}원"
                )
        self._prev_cash = balance.get("cash", 0) if total_asset > 0 else 0

        # ── 시장 상황 감지 (매 실행마다) ──
        market_condition = detect_market_condition(self.risk)
        self._market_condition = market_condition
        # tension 이상일 땐 일반주 신규 매수를 보수적으로 (확신도 낮으면 차단).
        # 인버스는 오히려 적극 매수하도록 gate 별도 우회.
        # defensive_mode_enabled=false면 시장 조건/drawdown 모두 무시하고 적극 회복.
        if not self.defensive_mode_enabled:
            self._defensive_mode = False
        else:
            self._defensive_mode = market_condition in ("tension", "crisis", "war")
            if market_condition != "peace":
                logger.info("시장 상황: %s — defensive 모드 활성", market_condition.upper())

            # 드로우다운 -10% 이상 시 defensive 강제 활성 (시장 조건과 무관하게 내부 방어)
            #: effective drawdown 사용 — 미국 T+2 미결제로 부풀려진 가짜 dd가
            # defensive를 잘못 트리거해 자본을 묶던 문제 해결.
            try:
                current_dd = self.tracker.get_effective_drawdown()
                if current_dd <= -10 and not self._defensive_mode:
                    self._defensive_mode = True
                    logger.warning("드로우다운 %.2f%% → defensive 모드 강제 활성", current_dd)
            except Exception:
                pass

        # ── 빠른 시장 급락 가드 (1-2 tick): 메가캡발/지수 급락 → 신규 진입 중단 + 방어 + 헷지 ──
        # 보유는 자르지 않는다(hold-floor 유지). 매수 게이트가 _fast_fall_active 를 보고 차단,
        # 인버스 진입 게이트는 이를 헷지 트리거로 사용.
        try:
            ff, ff_reason = self._fast_market_fall()
        except Exception:
            ff, ff_reason = False, ""
        was_active = getattr(self, "_fast_fall_active", False)
        self._fast_fall_active = ff
        if ff:
            self._defensive_mode = True
            if not was_active and self.discord:  # 활성 전환 시 1회 알림
                try:
                    self.discord.notify_error(f"급락 가드 발동 — 신규 진입 중단 + 헷지\n{ff_reason}")
                except Exception:
                    pass

        # 비용 절감: 방어/급락 모드면 전략을 로컬(adaptive)로 다운그레이드(비싼 Claude/4인 분석 OFF).
        # 매수는 확신 게이트에 막혀 어차피 안 사고, 보유 관리는 로컬 안전망이 담당 → 비싼 AI 낭비.
        # 크래시일수록 보유가 다 출혈→full 4인 분석 폭증(=agy/claude 소진)하던 비용원을 차단.
        if hasattr(self.strategy, "set_cheap_mode"):
            try:
                self.strategy.set_cheap_mode(
                    bool(getattr(self, "_defensive_mode", False)
                         or getattr(self, "_fast_fall_active", False)))
            except Exception:
                pass

        # ── 자산 + 시장 + 외부자산 기반 모드 자동 전환 ──
        external_reserve = self.config.get("external_reserve", 0)
        if total_asset > 0 and self.config.get("trading_mode") == "auto":
            new_mode = check_mode_change(
                self._active_mode, total_asset,
                market_condition=market_condition,
                discord=self.discord,
                external_reserve=external_reserve,
            )
            if new_mode:
                self._apply_mode_change(new_mode)

        # ── 현금 보유 비율 체크 (KR+US 통합 현금으로 판단) ──
        cash_reserve = self.config.get("_cash_reserve", 0)
        self._buy_blocked_low_cash = False
        if cash_reserve > 0 and total_asset > 0:
            min_cash = int(total_asset * cash_reserve)
            kr_cash = balance.get("cash", 0)
            # US 달러 예수금도 현금으로 카운트 (원화 환산)
            us_cash_krw = 0
            try:
                us_bal = self.client.get_us_balance()
                fx = self.client.get_usd_krw_rate()
                us_cash_krw = int(us_bal.get("cash_usd", 0) * fx)
            except Exception:
                pass
            total_cash = kr_cash + us_cash_krw
            if total_cash < min_cash:
                logger.info("현금 보유 부족: 통합 %s원 (KR %s + US %s) < 최소 %s (모드 %s, %.0f%%) — 매수 차단",
                            f"{total_cash:,}", f"{kr_cash:,}", f"{us_cash_krw:,}",
                            f"{min_cash:,}", self._active_mode, cash_reserve * 100)
                self._buy_blocked_low_cash = True

        # ── 일일 손실한도 동적 계산 ──
        loss_limit_pct = self.config.get("_daily_loss_limit_pct", 0.15)
        if total_asset > 0:
            self.risk.daily_loss_limit = -int(total_asset * loss_limit_pct)

        realized_today = self.tracker.get_realized_pnl_today()

        # 일일 목표 도달은 정보성 알림만 1회 발송 — 매매는 계속 진행하여 추가 수익 추구.
        #: 파일 가드로 재시작에도 1일 1회 유지. 기존 in-memory 플래그는 재시작마다
        # 리셋돼 같은 날 목표달성 알림이 재시작 횟수만큼 재발송(새벽 매도인데 밤에 또 뜸)되던 버그.
        if total_asset > 0 and self._daily_target_reached != today:
            #: 일일목표 판정·표시를 전체 계좌(KR+US) 기준으로 통일.
            # total_asset(KR-only)은 US 매도 실현을 KR 분모로 나눠 % 왜곡(+1.05% vs 실제 +0.75%).
            # compute_total_equity로 KR+US 통합 (실패 시 total_asset 폴백). 손실한도/현금예약은
            # total_asset 그대로(별도 영향 차단).
            _full_total = total_asset
            try:
                from zusik.analysis.bot_money_helpers import compute_total_equity
                _ft = int(compute_total_equity(
                    balance, self.client.get_us_balance(),
                    self.client.get_usd_krw_rate()).get("total", 0))
                if _ft > 0:
                    _full_total = _ft
            except Exception:
                pass
            _dt_guard = self._DAILY_TARGET_FILE
            _already_sent = False
            try:
                if os.path.exists(_dt_guard):
                    with open(_dt_guard) as _f:
                        _already_sent = (_f.read().strip() == today)
            except Exception:
                pass
            if _already_sent:
                self._daily_target_reached = today  # 오늘 이미 발송 → 재발송 안 함 (쿨다운만 유지)
            elif self.risk.check_daily_target_reached(realized_today["realized_pnl"], _full_total):
                self._daily_target_reached = today
                try:
                    os.makedirs("data", exist_ok=True)
                    with open(_dt_guard, "w") as _f:
                        _f.write(today)
                except Exception:
                    pass
                if self.discord:
                    rate = realized_today["realized_pnl"] / _full_total * 100
                    self.discord.notify_daily_target_reached(
                        realized_today["realized_pnl"], rate,
                        self.risk.daily_target_profit_rate * 100,
                    )
        self._daily_target_cooldown = (self._daily_target_reached == today)

        # 3) 일일 손실한도 — 시장별 판정. 한도 자체는 총자산 비례(위에서 계산)를 공유.
        # 평시(peace)에서는 인버스 헷지 손익을 판정에서 제외한다 — 헷지 손실은 보험료
        # 성격인데(급락 대비 비용) 그게 한도를 밀어 올려 반등장 전체를 정지시키던 문제.
        # 위기(peace 아님) 중에는 기존대로 전체 손익 기준(보수 유지).
        mkey = market or "ALL"
        _mc_now = getattr(self, "_market_condition", "peace")

        def _realized_for_halt() -> int:
            r = (self.tracker.get_realized_pnl_today(market=market)
                 if market else realized_today)
            pnl = r["realized_pnl"]
            if _mc_now == "peace":
                inv_pnl = sum(t.get("realized_pnl", 0) or 0 for t in r.get("details", [])
                              if self._is_inverse(t.get("code", "")))
                pnl -= inv_pnl
            return pnl

        if self._daily_loss_halted.get(mkey) == today:
            # 평시 복귀 자동 해제: 인버스 헷지 손익 제외 시 한도 내면 매매 재개.
            # 이후 판정도 같은 기준(_realized_for_halt)이라 flip-flop 없음 —
            # 인버스 외 손실이 한도를 넘으면 아래에서 즉시 재중단된다.
            if _mc_now == "peace" and not self.risk.check_daily_loss_limit(_realized_for_halt()):
                self._daily_loss_halted.pop(mkey, None)
                logger.info("[%s] 일일 손실한도 자동 해제 — 평시 복귀 + 인버스 헷지 비용 제외 손실 한도 내", mkey)
                if self.discord:
                    self.discord.notify_error(
                        f"[{mkey}] 손실한도 매매 중단 자동 해제 — 평시 복귀, "
                        f"인버스 헷지 비용 제외 손실이 한도 내 (매매 재개)"
                    )
            else:
                return False

        if self._daily_loss_released.get(mkey) == today:
            pass  # 운영자가 /손실해제 — 오늘은 이 시장 손실한도 재발동 안 함 (하드스톱/긴급홀딩은 별개)
        else:
            realized_for_market = _realized_for_halt()
            if self.risk.check_daily_loss_limit(realized_for_market):
                self._daily_loss_halted[mkey] = today
                if self.discord:
                    self.discord.notify_error(
                        f"[{mkey}] 일일 손실한도 도달: {realized_for_market:+,}원 — "
                        f"오늘 {mkey} 매매 중단 (/손실해제 로 재개 가능)"
                    )
                return False

        # 4) 전략 교체 체크
        if total_asset > 0:
            realized_total = self.tracker.get_realized_pnl_total()
            new_strategy_name = self.risk.check_strategy_switch(
                total_asset, realized_total["total_realized_pnl"]
            )
            if new_strategy_name:
                self._switch_strategy(new_strategy_name)

        return True

    def _check_crisis_with_data(self, df=None):
        """캔들 데이터로 위기 판단 + 종목 긴급 교체."""
        was_emergency = self.risk.is_emergency_hold()
        is_crisis = self.risk.check_crisis(df=df)

        if is_crisis and not was_emergency:
            # 신규 위기 감지 → 즉시 방어 종목으로 교체
            reason = self.risk.get_emergency_reason()
            if self.discord:
                self.discord.notify_emergency_hold(reason)

            if self.screener and self.auto_screen:
                self.screener.enter_crisis_mode()
                result = self.screener.screen_defensive(
                    self.kr_stocks, self.us_stocks, crisis_reason=reason,
                )
                self._apply_screened_stocks(result, tag="방어 종목 긴급 교체")

        # 긴급 홀딩 해제 조건: 최근 2일 양봉이면 해제
        if was_emergency and df is not None and len(df) >= 3:
            recent = df.tail(2)
            if all(recent["close"] > recent["open"]):
                logger.info("2일 연속 양봉 — 긴급 홀딩 해제 검토")
                self.risk.deactivate_emergency_hold()
                if self.screener:
                    self.screener.exit_crisis_mode()
                    self._refresh_stocks(force=True)  # 정상 종목으로 재선별
                if self.discord:
                    self.discord.notify_emergency_release()

    def _actively_trading(self, code: str) -> bool:
        """오늘 실제로 거래 중인가 — 거래정지/상폐 'LLM 확인'이 환각일 때 하드 데이터로 반증.

        체결가가 있고 오늘 거래량이 0보다 크면 거래정지가 아니다(정지 종목은 호가·체결이 0).
        조회 실패는 '알 수 없음' → False(가드 미작동, LLM 판단 존중)로 안전쪽 처리."""
        try:
            q = self.client.get_current_price(code)
            return float(q.get("price", 0) or 0) > 0 and float(q.get("volume", 0) or 0) > 0
        except Exception:
            return False

    def _safety_scan(self):
        """보유 종목 안전성 스캔.

        1. 종목별 손절선(-15%) 도달 → 강제 매도
        2. 상장폐지/관리종목 위험 감지 → 즉시 매도
        """
        try:
            balance = self.client.get_balance()
        except Exception:
            return

        for holding in balance.get("holdings", []):
            code = holding["code"]
            name = holding.get("name", code)
            qty = holding["qty"]
            if qty <= 0:
                continue

            # 1) 종목별 손절선
            if self.risk.check_stop_loss_per_stock(holding):
                logger.critical("강제 손절: %s — 손실 %.1f%%", name, holding["profit_rate"])
                result = self.client.sell_market(code, qty)
                if result.get("success"):
                    pnl = self.tracker.record_sell(
                        code, name, qty,
                        holding["current_price"], holding["avg_price"],
                        reason=f"강제 손절 (손실 {holding['profit_rate']:.1f}%)",
                    )
                    if self.discord:
                        self.discord.notify_trade(
                            side="sell", stock_name=name, stock_code=code,
                            qty=qty, price=holding["current_price"],
                            reason=f"강제 손절: 손실 {holding['profit_rate']:.1f}%",
                            realized_pnl=pnl["realized_pnl"],
                            realized_rate=pnl["realized_rate"],
                        )
                continue  # 손절 후 다음 종목

            # 2) 상장폐지/관리종목 위험 — 2단계 확인
            # 1단계: keyword substring 매칭으로 후보 추출
            # 2단계: LLM에게 "지금 실제로 위험 진행 중인가? Y/N" 직접 확인
            # → 부정·비교 문맥 false positive 차단
            if self.use_claude:
                try:
                    news = self.strategy.analyst.research_stock(code, name)
                    danger = self.risk.check_stock_danger(code, name, news)

                    if danger["is_dangerous"] and danger["action"] == "sell_immediately":
                        # 키워드 발견 → LLM 재확인
                        confirm = self.strategy.analyst.confirm_critical_danger(
                            code, name, danger["reasons"],
                        )
                        if not confirm["confirmed"]:
                            logger.info(
                                "위험 키워드 매칭됐으나 LLM 재확인 NO — 매도 차단: %s(%s) "
                                "키워드=%s 사유=%s",
                                name, code, danger["reasons"], confirm["reason"],
                            )
                        elif self._actively_trading(code):
                            # 오늘 정상 거래 중(체결가+거래량)이면 자동 매도를 보류하고 사람에게 알린다.
                            # 돌이킬 수 없는 강제매도를 일관성 없는 LLM 단독 판단에 맡기지 않는 게 핵심.
                            # 실측(2026-06-30): LLM 재확인이 1차 NO → 2차 YES 로 뒤집혀 거래 중 종목
                            # (256750)을 강제 매도. 거래정지면 애초에 체결이 0이라 못 팔고, 상폐
                            # 정리매매면 사람이 KRX 확인 후 수동 매도하는 게 안전하다.
                            logger.warning(
                                "위험 종목 자동매도 보류 — 오늘 거래 중: %s(%s) 키워드=%s. "
                                "사람 확인 필요(LLM 단독 판단으로 강제매도 안 함).",
                                name, code, danger["reasons"],
                            )
                            if self.discord:
                                self.discord.notify_error(
                                    f"위험 종목 확인 요망: {name}({code}) — 오늘 정상 거래 중인데 "
                                    f"{danger['reasons']} 신호. 자동 매도는 보류했습니다. KRX 공시로 "
                                    f"실제 거래정지/상장폐지(정리매매 포함)인지 확인 후 필요하면 수동 매도하세요.")
                        else:
                            if self.discord:
                                self.discord.notify_stock_danger(
                                    name, code, danger["danger_level"],
                                    danger["reasons"], danger["action"],
                                )
                            logger.critical(
                                "위험 종목 긴급 매도 (LLM 확인): %s — %s | %s",
                                name, danger["reasons"], confirm["reason"],
                            )
                            result = self.client.sell_market(code, qty)
                            if result.get("success"):
                                pnl = self.tracker.record_sell(
                                    code, name, qty,
                                    holding["current_price"], holding["avg_price"],
                                    reason=(f"위험 감지 긴급 매도(LLM확인): "
                                            f"{', '.join(danger['reasons'])} — {confirm['reason']}"),
                                )
                                if self.discord:
                                    self.discord.notify_trade(
                                        side="sell", stock_name=name, stock_code=code,
                                        qty=qty, price=holding["current_price"],
                                        reason=(f"위험 감지(LLM확인): "
                                                f"{', '.join(danger['reasons'])}"),
                                        realized_pnl=pnl["realized_pnl"],
                                        realized_rate=pnl["realized_rate"],
                                    )
                    elif danger["is_dangerous"]:
                        # warning 수준은 알림만 (action=hold)
                        if self.discord:
                            self.discord.notify_stock_danger(
                                name, code, danger["danger_level"],
                                danger["reasons"], danger["action"],
                            )
                    time.sleep(1)
                except Exception:
                    logger.warning("%s 위험도 조사 실패", name, exc_info=True)

    def _should_defer_sell(self, market: str, df, qty: float, avg_price: float,
                           current_price: float, confidence: float = 1.0) -> tuple[bool, str]:
        """로컬 연산으로 매도 연기 여부 판단.

        수수료·세금 공제 후 순익이 의미있고 + 상승 모멘텀이 약해야 실제 매도.
        confidence가 0.70 미만이면 LLM 신호 노이즈 가능성으로 보류 (5/1 강화).
        전략의 'sell' 신호 경로에만 적용 — 본전 보호/트레일링/손절선 같은
        안전장치는 이 gate를 거치지 않고 그대로 즉시 매도.
        """
        if qty <= 0 or avg_price <= 0 or current_price <= 0:
            return False, ""

        # LLM SELL 신호 신뢰도 게이트 (5/1 추가)
        # other 패턴 60% 승률 → 70% 미만 신호는 매도 보류, 안전망에 맡김
        if confidence < 0.70:
            return True, (f"LLM SELL conf {confidence * 100:.0f}% < 70% — "
                          f"신호 노이즈 가능성, 안전망(트레일링/본전보호)에 맡김")

        gross = (current_price - avg_price) * qty
        buy_fee = PortfolioTracker.estimate_fees(market, "buy", avg_price * qty)
        sell_fee = PortfolioTracker.estimate_fees(market, "sell", current_price * qty)
        net_pnl = gross - buy_fee - sell_fee
        invest = avg_price * qty
        net_rate = net_pnl / invest if invest > 0 else 0.0

        if net_pnl <= 0:
            return True, (f"순익 {net_pnl:+,.1f} ≤ 0 "
                          f"(왕복 수수료 {buy_fee + sell_fee:,.1f})")
        if net_rate < 0.003:
            return True, (f"순이익률 {net_rate * 100:+.2f}% < +0.30% "
                          f"(수수료 공제 후 수익 미미)")

        # 강한 익절 우선: 의미있는 순익(+3.5%↑)에서는 모멘텀(hold_score)이
        # 강해도 매도 연기하지 않는다. 고점에서 'sell' 신호가 와도 hold_score≥0.60에 막혀
        # 본전까지 흘려보내던 핵심 원인(고점 +8.9%→실현 +0.8% 등) 차단. 데이터: 고점 익절
        # (rsi_overbought 100% 건당+55k) ≫ 본전홀딩(breakeven 50% +772원). 안전망 아닌
        # 전략 'sell' 신호 경로에만 적용 — 라이딩은 surge/RSI트림이 별도 관리.
        strong_take = float(self.config.get("position", {}).get("strong_profit_take", 0.035))
        if net_rate >= strong_take:
            return False, (f"순익 {net_rate * 100:+.2f}% ≥ +{strong_take * 100:.1f}% "
                           f"— 고점 익절 우선 (모멘텀 게이트 무시)")

        from zusik.analysis.indicators import hold_score
        hs = hold_score(df)
        if hs["score"] >= 0.6:
            return True, (f"상승 지속 점수 {hs['score']:.2f} ≥ 0.60 "
                          f"(모멘텀 {hs['momentum']:+.2f}, MA_bull {hs['ma_bull']}, "
                          f"vol {hs['vol_ratio']:.2f}, pullback {hs['pullback']*100:.1f}%)")

        # 과매도 보호: RIVN -1,545 / SOFI -1,067 / SMCI -312 같은 패턴 차단.
        # RSI < 30 (극도의 과매도) + 음의 모멘텀이 강하지 않으면 → bottom 매도 의심 → 매도 보류
        try:
            if len(df) >= 15:
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(window=14, min_periods=14).mean()
                loss = (-delta.clip(upper=0)).rolling(window=14, min_periods=14).mean()
                # 횡보 데이터에서 gain=loss=0 → rs=NaN → RSI=NaN 또는 0. 무의미한 값은 skip
                if (loss.iloc[-1] or 0) > 0 or (gain.iloc[-1] or 0) > 0:
                    rs = gain / loss.replace(0, 1e-9)
                    rsi_now = float((100 - (100 / (1 + rs))).iloc[-1])
                    import math
                    if not math.isnan(rsi_now) and 0 < rsi_now < 30 and hs.get("momentum", 0.0) > -0.05:
                        return True, (f"과매도 보호: RSI {rsi_now:.1f} < 30 + 모멘텀 {hs['momentum']:+.2f} "
                                      f"— bottom 매도 의심 (반등 가능)")
        except Exception:
            pass

        return False, (f"net {net_rate * 100:+.2f}%, 상승점수 {hs['score']:.2f}")

    # ── 모호 케이스 LLM 라우팅 (pop-then-fade 익절 타이브레이크) ──
    # 데이터 근거: 진입 게이트 필터(국면/RS/MA60)는 승률을 못 올렸고, 부진 구간 거래를 단
    # 1건도 거르지 못했다 — 그 손실들은 RS양·MA위·모멘텀 다 충족한 "멀쩡한 돌파가 진입 후
    # 꺼진(pop-then-fade)" 것이라 진입 시점엔 구분 불가. 누수는 진입이 아니라 청산 문제.
    # 그 모호 케이스(고점에서 되돌렸지만 모멘텀이 애매해 로컬 게이트가 '연기'하는 구간)를
    # 규칙 대신 LLM 판단에 위임해 본전까지 흘러내리는 것을 끊는다.

    @staticmethod
    def _ambiguous_sell_band(net_rate: float, hold_score_val: float,
                             giveback: float, cfg: dict) -> bool:
        """이 매도가 LLM 타이브레이크를 부를 '모호 구간'인가(순수 판정, 무부작용).

        - 순익이 익절 가치 있는 구간(net_floor ≤ net < net_cap=strong_take 미만)
        - hold_score 가 경계(lo~hi) — 강한 모멘텀(>hi)은 추세지속이라 보유 신뢰
        - 고점 대비 되돌림 ≥ min_giveback (pop-then-fade 시그니처)
        """
        lo = float(cfg.get("hold_score_lo", 0.60))
        hi = float(cfg.get("hold_score_hi", 0.72))
        floor = float(cfg.get("net_floor", 0.003))
        cap = float(cfg.get("net_cap", 0.035))
        min_gb = float(cfg.get("min_giveback", 0.015))
        return (floor <= net_rate < cap
                and lo <= hold_score_val <= hi
                and giveback >= min_gb)

    @staticmethod
    def _parse_take_verdict(text: str):
        """LLM 응답에서 {action: take|hold, confidence, reason} 추출. 못 읽으면 None(보유 유지)."""
        if not text:
            return None
        import json
        import re
        try:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                d = json.loads(m.group(0))
                act = str(d.get("action", "")).strip().lower()
                if act in ("take", "hold"):
                    return {"action": act,
                            "confidence": float(d.get("confidence", 0) or 0),
                            "reason": str(d.get("reason", ""))[:120]}
        except Exception:
            pass
        return None  # 깨끗이 못 읽으면 보수적으로 None — 잘못된 매도 방지

    def _ambiguous_tiebreak_client(self):
        """타이브레이크용 LLM 클라이언트(지연 생성·캐시). 백엔드 없으면 None."""
        cl = getattr(self, "_tiebreak_client", None)
        if cl is None:
            try:
                from zusik.clients.claude_client import ClaudeClient
                cl = ClaudeClient(prefer_cli=True)
            except Exception:
                cl = False  # 생성 실패 표시(매번 재시도 안 함)
            self._tiebreak_client = cl
        return cl or None

    def _llm_profit_take_verdict(self, market, code, name, df, net_rate,
                                 peak, profit_rate, hs) -> dict | None:
        """모호 익절 구간에서 LLM 에 '지금 익절 vs 더 보유'를 묻는다(hard tier). 실패 시 None."""
        cl = self._ambiguous_tiebreak_client()
        if cl is None or not getattr(cl, "is_cli", False):
            return None
        try:
            recent = ", ".join(f"{int(v):,}" if v == v else "-"
                               for v in df["close"].tail(5).tolist())
        except Exception:
            recent = "-"
        prompt = (
            "다음 보유 종목을 지금 익절할지 더 보유할지 판단해라. 고점에서 되돌림이 나온 "
            "모호한(pop-then-fade) 구간이다.\n"
            f"종목: {name}({code}) | 시장: {market}\n"
            f"수수료 공제 순익률: {net_rate * 100:+.2f}%\n"
            f"최고수익 {peak * 100:+.2f}% → 현재 {profit_rate * 100:+.2f}% "
            f"(고점 대비 되돌림 {(peak - profit_rate) * 100:.2f}%p)\n"
            f"상승지속점수 {hs.get('score', 0):.2f} (모멘텀 {hs.get('momentum', 0):+.2f}, "
            f"MA강세 {hs.get('ma_bull')}, 거래량비 {hs.get('vol_ratio', 0):.2f}, "
            f"pullback {hs.get('pullback', 0) * 100:+.1f}%)\n"
            f"최근 5봉 종가: {recent}\n\n"
            "참고: 고점 익절(rsi_overbought/split_profit)은 실증 100% 승률이고, pop-then-fade "
            "방치는 본전까지 흘러내려 손실 누수의 핵심이었다. 단 진짜 추세 지속이면 보유가 큰 추세를 잡는다.\n"
            'JSON 한 줄만 출력: {"action":"take"|"hold","confidence":0~1,"reason":"간단히"}'
        )
        try:
            resp = cl.message(prompt, tier="hard")
        except Exception as e:
            logger.debug("모호 익절 LLM 호출 예외: %s", e)
            return None
        from zusik.clients.claude_client import ClaudeClient
        if ClaudeClient._is_failed(resp):
            return None
        return self._parse_take_verdict(resp)

    def _resolve_ambiguous_sell(self, market, df, code, name, qty, avg_price,
                                current_price, defer, reason):
        """로컬이 모멘텀(hold_score) 때문에 '연기'한 매도가 pop-then-fade 모호 구간이면
        LLM 판단으로 타이브레이크. take 판정이면 연기 해제(익절). 비활성/백엔드없음/실패/
        예외는 원래 (defer, reason) 그대로 — 무영향(fail-safe). 한 방향(hold→sell)만 뒤집는다."""
        cfg = self.config.get("ai_routing", {}) if isinstance(self.config, dict) else {}
        if not cfg.get("ambiguous_sell_enabled", True):
            return defer, reason
        if not defer:
            return defer, reason
        # 모멘텀 게이트로 연기된 경우에만. 노이즈(conf<70)/순손실/RSI 과매도 보호 연기는
        # 그대로 안전망에 맡긴다(손실 실현·바닥투매 방지 철학 유지).
        if "상승 지속 점수" not in (reason or ""):
            return defer, reason
        try:
            if qty <= 0 or avg_price <= 0 or current_price <= 0:
                return defer, reason
            gross = (current_price - avg_price) * qty
            buy_fee = PortfolioTracker.estimate_fees(market, "buy", avg_price * qty)
            sell_fee = PortfolioTracker.estimate_fees(market, "sell", current_price * qty)
            invest = avg_price * qty
            net_rate = (gross - buy_fee - sell_fee) / invest if invest > 0 else 0.0
            profit_rate = (current_price - avg_price) / avg_price
            peak = max(self.positions.get_peak_profit(code), profit_rate)
            giveback = peak - profit_rate

            from zusik.analysis.indicators import hold_score
            hs = hold_score(df)
            if not self._ambiguous_sell_band(net_rate, hs.get("score", 0.0), giveback, cfg):
                return defer, reason
            if self._ambiguous_cooldown_active(code, cfg):
                return defer, reason

            verdict = self._llm_profit_take_verdict(
                market, code, name, df, net_rate, peak, profit_rate, hs)
            self._mark_ambiguous_asked(code)
            if not verdict:
                return defer, reason
            min_conf = float(cfg.get("take_min_conf", 0.60))
            if verdict["action"] == "take" and verdict["confidence"] >= min_conf:
                logger.info("%s 모호익절 LLM=take (conf %.0f%%, peak %+.1f%%→%+.1f%%): %s",
                            name, verdict["confidence"] * 100, peak * 100,
                            profit_rate * 100, verdict["reason"])
                return False, (f"LLM 모호판정 익절 conf {verdict['confidence'] * 100:.0f}% "
                               f"(고점 {peak * 100:+.1f}%→{profit_rate * 100:+.1f}%): "
                               f"{verdict['reason']}")
            logger.info("%s 모호익절 LLM=hold (conf %.0f%%) — 보유 유지: %s",
                        name, verdict["confidence"] * 100, verdict["reason"])
            return True, f"{reason} + LLM 보유 유지"
        except Exception as e:
            logger.debug("모호 익절 판정 예외(%s): %s — 원판정 유지", code, e)
            return defer, reason

    def _ambiguous_cooldown_active(self, code: str, cfg: dict) -> bool:
        """같은 종목 LLM 타이브레이크 재질의 쿨다운(분). 매 사이클 호출 폭주·쿼터 보호."""
        import time
        ts = getattr(self, "_ambiguous_ask_ts", None)
        if ts is None:
            return False
        cd = float(cfg.get("cooldown_min", 0))  # 기본 0 = 매 tick 평가 허용
        if cd <= 0:
            return False
        return time.time() < ts.get(code, 0) + cd * 60

    def _mark_ambiguous_asked(self, code: str):
        import time
        ts = getattr(self, "_ambiguous_ask_ts", None)
        if ts is None:
            ts = {}
            self._ambiguous_ask_ts = ts
        ts[code] = time.time()

    @staticmethod
    def _stale_rotate_due(age_hours: float, profit_rate: float, hold_score_val: float) -> bool:
        """죽은 포지션 회전 청산 정책: 72h+ 보유 AND 모멘텀 소멸(hold_score<0.45)
        AND **본전 회복 구간(-0.5%~+2%)**일 때만 True — 손실 실현 절대 안 함.

        실측: 보유 1h-1d 매도 +634k(건당 +18k) vs 1-3d -299k, 3-7d -92k — 모멘텀 매수가
        1일 내 안 터지면 죽은 자본이 됨. 단 얕은 손실 컷은 41% 정확도(바닥투매)라 금지 —
        대신 본전 반등에서 강세에 팔아(본전보호 77%승률 +55k 패턴과 정합) 자본을
        승자 버킷으로 재배치. 손실 중이면 기존 hold floor가, +2%↑면 익절 로직이 담당.
        """
        return (age_hours >= 72.0
                and -0.005 <= profit_rate < 0.02
                and hold_score_val < 0.45)

    def _check_stale_rotate(self, symbol: str, profit_rate: float, df) -> bool:
        """회전 청산 실행 판정 — 마지막 매수 후 경과시간 + hold_score 로컬 계산."""
        try:
            last = self.tracker.get_last_buy_time(symbol)
            if not last:
                return False
            age_h = (datetime.now() - last).total_seconds() / 3600.0
            from zusik.analysis.indicators import hold_score as _hs
            hs = float(_hs(df).get("score", 0.5))
            return self._stale_rotate_due(age_h, profit_rate, hs)
        except Exception:
            return False

    @staticmethod
    def _trailing_fire_allowed(from_high: float, profit_rate: float) -> bool:
        """인라인 트레일링(US) 발동 정책: 고점 -10%↓ **그리고 순익(+0.5%↑)**.

        트레일링 = 수익 보호 장치. 손실 상태 발동 = 변질된 손절컷 — 실측 발동 2건 전패
        -264k(델 -76k HPE -187k @-8.4%). 손실 구간 자본보호는
        _hold_through_loss floor(-9%/-15%)와 하드스톱이 담당한다.
        """
        return from_high <= -0.10 and profit_rate > 0.005

    def _index_crash(self) -> bool:
        """지수 프록시(KR 069500 / US QQQ·SPY)가 '갑작스러운 sharp 급락' 중인가.

        기준: 1일 ≤ -3.5% 또는 3일 누적 ≤ -7% (지수 단위 — 개별주 아님).
        인버스 재설계 근거(inverse_backtest.py, KIS 2년):
          - 강세장 pullback(-1~2%)을 인버스로 추격하면 -23~-37% 손실(전부 회복).
          - 어떤 모멘텀/추세 임계도 강세장에선 손실 → '진짜 급락'에만 반응해야 함.
          - 지수 -3.5%/일·-7%/3일은 강세장에서 거의 안 나오는 sharp 급락 = 진짜 하락장 신호.
        10분 캐시 (Claude 無, 로컬).
        """
        import time as _t
        now = _t.time()
        ts, cached = getattr(self, "_crash_cache", (0.0, False))
        if now - ts < 600 and ts > 0:
            return cached

        def _sharp(df) -> bool:
            if df is None or len(df) < 4:
                return False
            c = df["close"].values
            r1 = (c[-1] - c[-2]) / c[-2] if c[-2] > 0 else 0.0
            r3 = (c[-1] - c[-4]) / c[-4] if c[-4] > 0 else 0.0
            return r1 <= -0.035 or r3 <= -0.07

        # 장중 급락 보강: 일봉은 진행 중인 당일 폭락을 반영 못 한다
        # (폭락일 _index_crash=False → 인버스 매수 0건 버그). 지수 프록시 '현재가 등락률'을
        # 직접 본다. 광역지수 -3%/일중은 강세장 pullback(-1~2%)을 한참 넘는 진짜 급락.
        INTRADAY_TH = -3.0  # %

        crash = False
        try:
            for code, _n in self._INDEX_PROXIES_KR:
                try:
                    cr = float(self.client.get_current_price(code).get("change_rate", 0) or 0)
                    if cr <= INTRADAY_TH:
                        crash = True; break
                except Exception:
                    pass
                if _sharp(self.client.get_ohlcv(code)):
                    crash = True; break
            if not crash:
                for tk, ex in self._INDEX_PROXIES_US:
                    try:
                        cr = float(self.client.get_us_current_price(tk, ex).get("change_rate", 0) or 0)
                        if cr <= INTRADAY_TH:
                            crash = True; break
                    except Exception:
                        pass
                    if _sharp(self.client.get_us_ohlcv(tk, exchange=ex)):
                        crash = True; break
        except Exception:
            pass
        self._crash_cache = (now, crash)
        if crash:
            logger.info("지수 급락 감지 (_index_crash=True) — 인버스 헷지 게이트 open")
        return crash

    def _fast_market_fall(self) -> tuple[bool, str]:
        """1-2 tick 내 '빠른 시장 급락' 감지 — 지수 단독 급락 또는 메가캡발 시장 급락.

        사용자 선택(조합: 매수차단+방어 & 헷지) 구현: 발동 시 신규 진입을 중단하고 인버스 헷지를
        열되, **보유 종목은 자르지 않는다**(hold-floor 유지 — 빠른 손절은 과거 0% 승률).
        열린 시장의 프록시만 본다(닫힌 시장의 stale 등락률로 오발 방지). ~2분 캐시(run_once≈1 tick).

        트리거:
          1) 지수 프록시 장중 ≤ index_sharp_pct (기본 -2.5%)  — 지수 단독 급락
          2) 美 메가캡(_MEGACAP_LEADERS) 장중 ≤ megacap_drop_pct (기본 -3.5%)
             AND 지수 동반 ≤ index_confirm_pct (기본 -1.0%)   — '메가캡발 시장 급락'
        """
        cfg = (self.config.get("risk", {}) or {}).get("fast_fall_guard", {}) or {}
        if not cfg.get("enabled", True):
            return False, ""
        import time as _t
        now = _t.time()
        ts, cached = getattr(self, "_fastfall_cache", (0.0, (False, "")))
        if now - ts < 120 and ts > 0:
            return cached

        idx_sharp = float(cfg.get("index_sharp_pct", -2.5))
        idx_confirm = float(cfg.get("index_confirm_pct", -1.0))
        megacap_drop = float(cfg.get("megacap_drop_pct", -3.5))

        try:
            kr_open = self.client.is_market_open()
        except Exception:
            kr_open = False
        try:
            us_open = self.client.is_us_market_open()
        except Exception:
            us_open = False

        index_changes: list[tuple] = []   # (label, pct)
        megacap_worst = None              # (ticker, pct)
        if kr_open:
            for code, name in self._INDEX_PROXIES_KR:
                try:
                    cr = float(self.client.get_current_price(code).get("change_rate", 0) or 0)
                    index_changes.append((name, cr))
                except Exception:
                    pass
        if us_open:
            for tk, ex in self._INDEX_PROXIES_US:
                try:
                    cr = float(self.client.get_us_current_price(tk, ex).get("change_rate", 0) or 0)
                    index_changes.append((tk, cr))
                except Exception:
                    pass
            for tk in getattr(self, "_MEGACAP_LEADERS", []):
                try:
                    cr = float(self.client.get_us_current_price(tk, "NASD").get("change_rate", 0) or 0)
                    if megacap_worst is None or cr < megacap_worst[1]:
                        megacap_worst = (tk, cr)
                except Exception:
                    pass

        worst_idx = min(index_changes, key=lambda x: x[1]) if index_changes else None
        result = (False, "")
        if worst_idx and worst_idx[1] <= idx_sharp:
            result = (True, f"지수 급락 {worst_idx[0]} {worst_idx[1]:+.1f}% — 신규 진입 중단 + 헷지")
        elif (megacap_worst and megacap_worst[1] <= megacap_drop
              and worst_idx and worst_idx[1] <= idx_confirm):
            result = (True, f"메가캡 {megacap_worst[0]} {megacap_worst[1]:+.1f}%발 시장 급락 "
                            f"(지수 {worst_idx[0]} {worst_idx[1]:+.1f}%) — 신규 진입 중단 + 헷지")

        self._fastfall_cache = (now, result)
        if result[0]:
            logger.warning("급락 가드 발동: %s", result[1])
        return result

    def _amend_stale_limit_orders(self, timeout_sec: int = 60):
        """미체결 지정가 주문을 시장가로 정정 (키움 ch8 패턴).

        지정가가 timeout_sec 이상 미체결이면 ord_dvsn=01(시장가) 정정 주문을 던진다.
        시장가 위주인 현 코드에선 보통 no-op. KR 정규장(09:00~15:20)에서만 동작.

        실패해도 봇 메인 루프를 멈추지 않도록 모든 예외 흡수.
        """
        try:
            stale = self.order_guard.get_stale_orders(timeout_sec=timeout_sec)
        except Exception:
            return
        if not stale:
            return
        try:
            kr_open = self.client.is_market_open()
        except Exception:
            kr_open = False
        if not kr_open:
            return
        for o in stale:
            if o.get("market") != "KR":
                continue  # US는 별도 채널 (시장가 정정 미지원)
            code = o.get("code", "")
            order_no = o.get("order_no", "")
            qty = int(o.get("qty", 0) or 0)
            if not code or not order_no or qty <= 0:
                continue
            try:
                logger.info("지정가 미체결 %ds 초과 → 시장가 정정: %s 주문 %s",
                            timeout_sec, code, order_no)
                res = self.client.revise_order(code, order_no, qty, price=0,
                                               order_type="01")
                if res.get("success"):
                    self.order_guard.mark_amended(order_no, res.get("order_no", ""))
                    if self.discord:
                        try:
                            self.discord.notify_info(
                                f"지정가 미체결 정정 → 시장가: {code} {qty}주"
                            )
                        except Exception:
                            pass
                else:
                    logger.warning("정정 실패 %s: %s", code, res.get("message", ""))
            except Exception as e:
                logger.warning("정정 예외 %s: %s", code, str(e)[:120])

