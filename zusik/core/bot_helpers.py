from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from zusik import paths


logger = logging.getLogger(__name__)

#: 캘리브레이션 학습 파라미터 파일 (bot.py 의 _LEARNED_PARAMS_FILE 과 동일 경로).
# 런타임 재적용(_refresh_learned_params)이 mtime 으로 갱신을 감지하는 대상.
_LEARNED_PARAMS_FILE = os.path.join("data", "learned_params.json")

# 데일리 Claude 편향(verdict) → 매매 효과. 생산자(_parse_daily_bias)의 허용 어휘와
# 소비자(_ai_signal_for)의 효과 매핑을 한 곳에서 정의해 둘이 어긋나지 않게 한다.
#   값: (size_mult, floor_relief, min_floor, block)
_DAILY_BIAS_EFFECT = {
    "buy":        (1.15, 0.10, 0.0, False),
    "strong_buy": (1.15, 0.10, 0.0, False),
    "accumulate": (1.15, 0.10, 0.0, False),
    "hold":       (1.0, 0.0, 0.0, False),
    "neutral":    (1.0, 0.0, 0.0, False),
    "reduce":     (0.80, 0.0, 0.65, False),
    "trim":       (0.80, 0.0, 0.65, False),
    "caution":    (0.80, 0.0, 0.65, False),
    "lighten":    (0.80, 0.0, 0.65, False),
    "sell":       (1.0, 0.0, 0.0, True),
}
DAILY_BIAS_VERDICTS = frozenset(_DAILY_BIAS_EFFECT)


# 안전한 adaptive 상태 trigger 평가 — eval() 대체.
# 허용 문법: "default" | "<metric> <op> <number>" (and/or 결합 가능).
#   metric ∈ {dd, pnl}, op ∈ {<=, >=, ==, !=, <, >}.
# 기존 eval(expr, {"__builtins__": {}}, {}) 는 builtins 를 막아 RCE 는 차단했으나,
# config.local.yaml 을 쓸 수 있는 공격자가 복잡한 표현식으로 CPU 소모/예외/의도치 않은
# 상태 전환을 유발할 여지가 있었다. 문법을 비교식으로 제한해 그 표면을 제거한다.
_TRIGGER_OPS = {
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
}
_TRIGGER_ATOM = re.compile(r"^\s*(dd|pnl)\s*(<=|>=|==|!=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")


def _eval_trigger_atom(atom: str, dd: float, pnl: float):
    """단일 비교식 평가. 문법 외면 None(평가 불가)."""
    m = _TRIGGER_ATOM.match(atom)
    if not m:
        return None
    metric, op, num = m.group(1), m.group(2), float(m.group(3))
    val = dd if metric == "dd" else pnl
    return _TRIGGER_OPS[op](val, num)


def eval_adaptive_trigger(trigger: str, dd: float, pnl: float) -> bool:
    """adaptive 상태 trigger 안전 평가. 문법 외/오류는 fail-safe 로 False."""
    t = (trigger or "").strip()
    if t == "default":
        return True
    if not t:
        return False
    # Python 의미와 동일하게 and 가 or 보다 강하게: or 로 먼저 쪼갠 뒤 각 항을 and 평가.
    for or_term in t.split(" or "):
        ok = True
        for atom in or_term.split(" and "):
            res = _eval_trigger_atom(atom, dd, pnl)
            if not res:  # None(문법 외) 또는 False → 이 항 실패
                ok = False
                break
        if ok:
            return True
    return False


class CoreHelpersMixin:
    """CoreHelpersMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _load_last_commit_hash(self) -> str:
        """파일에서 마지막 커밋 해시 로드."""
        try:
            if os.path.exists(self._LAST_COMMIT_FILE):
                with open(self._LAST_COMMIT_FILE) as f:
                    return f.read().strip()
        except Exception:
            pass
        return ""

    def _save_last_commit_hash(self, h: str):
        """마지막 커밋 해시를 파일에 저장 (재시작 시 복원)."""
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._LAST_COMMIT_FILE, "w") as f:
                f.write(h)
        except Exception:
            pass

    def _refresh_learned_params(self):
        """learned_params.json 이 갱신됐으면(mtime 변화) 재시작 없이 런타임 재적용.

        calibrate_from_history.py(주기 타이머, deploy/zusik-calibrate.timer)가 청산 파라미터를
        다시 학습해 파일을 쓰면, 이 메서드가 다음 tick 에 PositionManager 에 오버레이한다 —
        예전엔 load_config 가 시작 시에만 읽어 '봇이 몇 주째 재시작 안 하면 fresh 학습이 안 먹던'
        stale 갭이 있었다(사용자 지적). mtime 비교라 비용 거의 0. 화이트리스트 키만(자본보호 불변).
        """
        fp = _LEARNED_PARAMS_FILE
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            return  # 파일 없음 — config.yaml 기본값 유지
        if getattr(self, "_learned_mtime", None) == mtime:
            return  # 변화 없음
        try:
            with open(fp, encoding="utf-8") as f:
                learned = json.load(f)
        except Exception as e:
            logger.warning("학습 파라미터 런타임 로드 실패: %s — 무시", e)
            return
        applied = self.positions.apply_learned_params(learned)
        self._learned_mtime = mtime
        if applied:
            logger.info("학습 파라미터 런타임 재적용(%s): %s — 재시작 불요",
                        learned.get("calibrated_at", "?"), applied)

    def _check_new_commits(self):
        """새 커밋 감지 → 메신저 알림 + 반영 여부 선택.

        **로컬 커밋·원격 push 둘 다 알림**. 같은 서버에서 commit+push 하면 로컬==origin 이라
        예전엔(origin-ahead 만 봐서) 알림이 안 떴음 → 최신 커밋(원격이 앞서면 추적 브랜치,
        아니면 로컬 HEAD) 이 직전 알린 것과 다르면 알린다. 적용은 Discord 버튼 또는 '업데이트'
        명령(git pull + 재시작). 로컬 커밋은 pull 할 게 없고 재시작으로 반영됨.
        """
        try:
            import subprocess
            from zusik.paths import ROOT
            repo_dir = str(ROOT)

            # 원격 변경사항 가져오기 (merge 안 함). 오프라인이어도 로컬 HEAD 로 계속 진행.
            subprocess.run(
                ["git", "fetch", "--quiet"],
                capture_output=True, text=True, timeout=15, cwd=repo_dir,
            )

            # 추적 브랜치를 동적으로 감지 — 공개 저장소는 main, dev 는 master 라
            # origin/master 하드코딩이면 main 클론에서 원격 커밋을 영영 못 본다.
            up = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True, text=True, timeout=5, cwd=repo_dir,
            )
            upstream = (up.stdout.strip()
                        if up.returncode == 0 and up.stdout.strip() else "origin/master")

            # 원격이 로컬보다 앞선 커밋 수 (당겨올 push). >0 이면 원격, 아니면 로컬 HEAD 기준.
            behind = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..{upstream}"],
                capture_output=True, text=True, timeout=5, cwd=repo_dir,
            )
            n = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else 0
            ref = upstream if n > 0 else "HEAD"

            # %h hash, %s subject, %an author, %cr relative date, %cI ISO date
            result = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%h|%s|%an|%cr|%cI", ref],
                capture_output=True, text=True, timeout=5, cwd=repo_dir,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return
            current = result.stdout.strip()
            commit_hash = current.split("|")[0]

            last = getattr(self, "_last_commit_hash", None)
            if last is None:
                last = self._load_last_commit_hash()
                self._last_commit_hash = last
            if commit_hash == last:
                return  # 이미 알린 커밋

            parts = current.split("|", 4)
            h, msg, author, when = parts[0], parts[1], parts[2], parts[3]
            commit_iso = parts[4] if len(parts) > 4 else ""
            files = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", ref],
                capture_output=True, text=True, timeout=5, cwd=repo_dir,
            ).stdout.strip()
            file_list = ", ".join(files.split("\n")[:5])

            # Discord: 인터랙티브 버튼(적용/스킵). 다른 메신저: 텍스트 알림('업데이트' 명령으로 반영).
            from zusik.clients import discord_bot as _db
            sent = False
            try:
                sent = bool(_db.send_update_alert(h, msg, author, when, file_list, commit_iso=commit_iso))
            except Exception:
                pass

            # 재시작 직후 첫 tick 은 Discord 봇이 아직 on_ready 전이라 채널 미연결(channel=False) →
            # 발송 스킵된다. 그때 해시를 저장하면 notified 처리돼 영영 재시도 못 하고 알림 유실.
            # 봇 토큰은 있는데 채널 연결 전이면 '보류' → 해시 저장 안 하고 다음 tick(연결 후) 재시도.
            bot_ref = getattr(_db, "_discord_bot_ref", None)
            bot_connecting = bool(bot_ref) and not getattr(bot_ref, "_alert_channel", None)
            if not sent and bot_connecting:
                logger.info("새 커밋 알림 보류 — Discord 봇 채널 미연결, 다음 tick 재시도: %s", h)
                return

            try:
                self.discord.notify_update_available(
                    commit_hash=h, msg=msg, author=author, when=when, files=file_list, count=n)
            except Exception:
                pass

            self._last_commit_hash = commit_hash
            self._save_last_commit_hash(commit_hash)
            logger.info("새 커밋 알림: %s — %s %s",
                        f"origin {n} commits ahead(pull 가능)" if n > 0 else "로컬 커밋(재시작 반영)",
                        h, msg)
        except Exception:
            pass

    def _cash_idle_hours(self) -> int:
        """보유 0 상태가 몇 시간째인지 (마지막 매도 또는 장 시작 이후)."""
        try:
            # 마지막 매도 시각
            sells = [t for t in self.tracker._trades if t["type"] == "sell"]
            if sells:
                last_sell = datetime.fromisoformat(sells[-1]["timestamp"])
                return int((datetime.now() - last_sell).total_seconds() / 3600)
            # 매도 이력 없으면 장 시작(09:00)부터
            now = datetime.now()
            market_open = now.replace(hour=9, minute=0, second=0)
            if now > market_open:
                return int((now - market_open).total_seconds() / 3600)
        except Exception:
            pass
        return 0

    def _record_buy_with_context(self, code: str, name: str, qty: int, price,
                                 is_long_term: bool, reason: str,
                                 entry_context: str = "",
                                 entry_indicators: dict | None = None,
                                 entry_analyst_details: dict | None = None):
        """PortfolioTracker 신·구 시그니처를 모두 지원하는 매수 기록 어댑터.

        실운영 Tracker는 진입 컨텍스트를 원자적으로 저장한다. 오래된 플러그인이나 테스트
        Tracker가 새 키워드를 모르면 기존 6인자 호출로 폴백한다.
        """
        import inspect
        fn = self.tracker.record_buy
        try:
            signature_params = inspect.signature(fn).parameters
            supports_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in signature_params.values()
            )
        except (TypeError, ValueError):
            signature_params = {}
            supports_kwargs = False
        metadata = {
            "entry_context": entry_context,
            "entry_indicators": entry_indicators,
            "entry_analyst_details": entry_analyst_details,
        }
        compatible = {
            key: value for key, value in metadata.items()
            if supports_kwargs or key in signature_params
        }
        return fn(code, name, qty, price, is_long_term, reason, **compatible)

    def _last_entry_context(self, code: str) -> str:
        """구형 Tracker 호환 진입 컨텍스트 조회."""
        fn = getattr(self.tracker, "get_last_buy_context", None)
        if callable(fn):
            try:
                return str(fn(code) or "")
            except Exception:
                pass
        return ""

    def _last_entry_indicators(self, code: str) -> dict:
        fn = getattr(self.tracker, "get_last_buy_indicators", None)
        if callable(fn):
            try:
                value = fn(code)
                return dict(value) if isinstance(value, dict) else {}
            except Exception:
                pass
        return {}

    def _last_entry_analyst_details(self, code: str) -> dict:
        fn = getattr(self.tracker, "get_last_buy_analyst_details", None)
        if callable(fn):
            try:
                value = fn(code)
                return dict(value) if isinstance(value, dict) else {}
            except Exception:
                pass
        return {}

    def _get_name(self, code: str) -> str:
        if code not in self._name_cache:
            try:
                self._name_cache[code] = self.client.get_stock_name(code)
            except Exception:
                self._name_cache[code] = code
        return self._name_cache[code]

    @staticmethod
    def _is_weak_trend(df) -> tuple[bool, str]:
        """추세 필터: 데드크로스(5일선 < 20일선) + 60일선 아래.

        둘 다 만족하면 약세 추세 → 매수 차단. 솔루스/SMCI 같은 추세 하락
        종목에 LLM이 BUY 추천해도 로컬 가드로 사전 차단.
        """
        if df is None or len(df) < 60:
            return False, ""
        try:
            close = df["close"]
            ma5 = float(close.rolling(5).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma60 = float(close.rolling(60).mean().iloc[-1])
            cur = float(close.iloc[-1])
            if ma5 < ma20 and cur < ma60:
                return True, (
                    f"약세 추세 (5일선 {ma5:.0f} < 20일선 {ma20:.0f}, "
                    f"현재가 {cur:.0f} < 60일선 {ma60:.0f})"
                )
        except Exception:
            pass
        return False, ""

    def _ensure_ws_subscription(self, code: str, market: str = "KR",
                                exchange: str = "NASD"):
        """변동성 extreme tier 보유 종목에 WebSocket 실시간 구독 등록.

        체결 틱이 들어올 때마다 직전 매수가 대비 ±X% 변동 시 즉시 매수/매도 트리거.
        분봉 대기 없이 sub-second 반응. 인증 실패 시 조용히 분봉 폴백.
        """
        if code in self._ws_subscribed:
            return
        if not (self.config.get("realtime", {}) or {}).get("enabled", True):
            return  # 실시간 WS 비활성(config) — 분봉 폴백만
        try:
            if self._ws_manager is None:
                from zusik.clients.kis_websocket import KISWebSocketManager
                self._ws_manager = KISWebSocketManager(
                    app_key=os.environ.get("KIS_APP_KEY", ""),
                    app_secret=os.environ.get("KIS_APP_SECRET", ""),
                    is_virtual=bool(getattr(self.client, "is_virtual", False)),
                )
                if not self._ws_manager.start():
                    logger.info("WebSocket 비활성 — 분봉 폴백 사용")
                    self._ws_manager = None
                    return

            # 직전 틱 가격 추적 (스파이크 감지용)
            tick_state = {"last_price": 0.0, "peak": 0.0}

            def _on_tick(msg: dict):
                """체결 틱 콜백 — 빠른 손절/익절 트리거."""
                try:
                    price = msg.get("price", 0)
                    if price <= 0:
                        return
                    last = tick_state["last_price"] or price
                    tick_state["last_price"] = price
                    tick_state["peak"] = max(tick_state["peak"], price)

                    # 1초 사이 가격 -1.5% 이상 급변 → 즉시 매도 검토
                    pct_change = (price - last) / last if last > 0 else 0
                    from_peak = (price - tick_state["peak"]) / tick_state["peak"] if tick_state["peak"] > 0 else 0

                    # 보유 종목 정보 조회
                    if market == "KR":
                        bal = self.client.get_balance()
                        h = next((x for x in bal["holdings"] if x["code"] == code), None)
                    else:
                        bal = self.client.get_us_balance()
                        h = next((x for x in bal["holdings"] if x["ticker"] == code), None)
                    if not h or h.get("qty", 0) <= 0:
                        return

                    pr = (h.get("profit_rate", 0) or 0) / 100

                    # 상승 급등 실시간 익절 (하락 보호와 대칭, — 5분 run_once 대기 없이 즉시.
                    # 기존 _on_tick은 하락(급락 매도)만 처리하고 상승 익절(check_surge)은 5분 주기에만
                    # 있어, 사이클 중 급등→되돌림이면 익절을 놓쳤다(수익 놓침). split_profit/rsi_overbought
                    # 익절은 실증 100% 승률이라 실시간화가 손익에 +. per-code 60s 스로틀로 틱폭주/중복 방지.
                    if pr > 0:
                        import time as _ts
                        _now_s = _ts.time()
                        if _now_s - self._tick_surge_throttle.get(code, 0.0) >= 60:
                            surge = self.positions.check_surge(code, int(price))
                            if surge:
                                self._tick_surge_throttle[code] = _now_s
                                logger.info("틱 급등 익절: %s +%.1f%% (%s, ratio %.2f) — 즉시",
                                            code, pr * 100, surge.get("action", "익절"),
                                            surge.get("sell_ratio", 1.0))
                                if market == "KR":
                                    self._handle_sell(code, h.get("name", code),
                                                      force_reason=f"{surge['reason']} (틱)",
                                                      sell_ratio=surge.get("sell_ratio", 1.0))
                                else:
                                    self._us_force_sell_reason = f"{surge['reason']} (틱)"
                                    try:
                                        self._handle_us_sell(code, h.get("name", code),
                                                             h.get("exchange", "NASD"),
                                                             sell_ratio=surge.get("sell_ratio", 1.0))
                                    finally:
                                        self._us_force_sell_reason = ""
                                return

                    # 틱 트리거 — crash_instant 0%승률/-546k 주범 제거):
                    # 기존 "고점 대비 -2% + 손실 -1%"는 정상 노이즈에 전량 투매했다.
                    # 특히 갓 매수한 종목(삼성/LG이노텍 등)을 일중 -2% 출렁임에 바닥 투매.
                    # 이제 진짜 급락만 발동:
                    # - fast_crash: 직전 틱 대비 -1.5% 이상 급변(설계 의도) + 손실 -2% 이상
                    # - deep_reversal: 고점 대비 -4% 붕괴 + 손실 -3% 이상
                    # + 매수 직후 30분 보호 (급락 손절 경로와 동일 — buy→sell 분단위 churn 방지).
                    # 느린 하락은 5분봉/급락/트레일링/-15% 하드스톱이 별도로 잡음.
                    #: deep_reversal -4%/-3% → -6%/-4% (정상 pullback에 너무 급하게
                    # 잡던 문제 — 하이닉스가 고점대비 -4%/손익 -3%에 틱 매도됨).
                    fast_crash = pct_change <= -0.015 and pr <= -0.02
                    deep_reversal = from_peak <= -0.06 and pr <= -0.04
                    trigger = (fast_crash or deep_reversal) and not self._is_recently_bought(code, minutes=30)
                    # 핵심주(whitelist)는 틱 급락(이벤트성 pullback)에 안 던짐 — -15%↓ 깊은 손실만.
                    # crash/quick_loss/slow_bleed 면제와 동일 원칙. 하이닉스 -4% 틱 매도 재발 방지.
                    # 억제: 핵심 -15% / 비핵심 KR -9%(config)까지 틱 pullback 홀드.
                    # US는 기존 동작(핵심만 면제) 유지. crash_instant 0%승률 바닥투매 방지.
                    if trigger and self._hold_through_loss(code, pr):
                        # 종목별 60초 스로틀 — 폭락 시 초당 수십 틱마다 동일 로그가 찍혀
                        # 로그가 하루 2.4만 줄/85MB로 폭증하던 문제 차단.
                        import time as _tt
                        _now_t = _tt.time()
                        _last_t = self._tick_exempt_logged.get(code, 0.0)
                        if _now_t - _last_t >= 60:
                            logger.info("틱 급락 면제(조기손절 억제): %s (peak대비 %+.1f%%, 손익 %+.1f%%) — pullback 홀드",
                                        code, from_peak * 100, pr * 100)
                            self._tick_exempt_logged[code] = _now_t
                        trigger = False
                    if trigger:
                        logger.warning("틱 급락: %s %.2f → %.2f (peak %.2f, 1틱 %+.2f%%, peak대비 %+.2f%%, 손익 %+.1f%%) — 즉시 매도",
                                       code, last, price, tick_state["peak"], pct_change * 100, from_peak * 100, pr * 100)
                        if market == "KR":
                            self._handle_sell(code, h.get("name", code),
                                              force_reason=f"틱 급락 (peak {from_peak*100:+.1f}%)")
                        else:
                            exch = h.get("exchange", "NASD")
                            self._handle_us_sell(code, h.get("name", code), exch)
                except Exception as e:
                    logger.debug("WS 틱 콜백 오류: %s", e)

            self._ws_manager.subscribe(code, _on_tick, market=market, exchange=exchange)
            self._ws_subscribed.add(code)
            logger.info("WebSocket 구독: %s (%s, extreme tier)", code, market)
        except Exception as e:
            logger.debug("WebSocket 구독 실패 %s: %s", code, e)

    def _compute_mc_stats(self, df, n_paths: int = 10000, t_forward: int = 30):
        """Monte Carlo bootstrap 통계 — 동적 임계 자동 적용.

        종목 변동성 + 시장 condition 기반으로 stop_loss/trailing/target 자동 결정.
        예: 저변동주는 -5%/+5%, 고변동주는 -12%/+12%.
        """
        try:
            from zusik.analysis.bot_money_helpers import compute_returns_from_ohlcv, run_mc_with_fallback
            from zusik.core.dynamic_thresholds import compute_dynamic_thresholds

            returns = compute_returns_from_ohlcv(df, lookback=60)
            if returns is None:
                return None

            # 동적 임계 계산 (변동성 + 시장 condition)
            thresholds = compute_dynamic_thresholds(
                df, market_condition=getattr(self, "_market_condition", "peace"),
            )
            mc = run_mc_with_fallback(
                returns, None,
                n_paths=n_paths, t_forward=t_forward,
                stop_loss=thresholds["stop_loss"],
                trailing_stop=thresholds["trailing_stop"],
                target_profit=thresholds["target_profit"],
            )
            if mc is not None:
                # 동적 임계도 결과에 포함 (Kelly/매도 게이트가 활용)
                mc["dynamic_thresholds"] = thresholds
            return mc
        except Exception as e:
            logger.debug("MC 계산 오류: %s", e)
            return None

    @staticmethod
    def _format_mc_for_llm(mc: dict) -> str:
        from zusik.analysis.bot_money_helpers import format_mc_for_llm
        return format_mc_for_llm(mc)

    def _mc_buy_gate(self, code: str, name: str, df) -> tuple[bool, str]:
        """MC 매수 게이트. 결과는 self._last_mc_stats에 캐시."""
        if df is None or len(df) < 30 or self._is_inverse(code):
            return True, "인버스/데이터 부족 — 우회"
        gate_cfg = (self.config.get("risk", {}) or {}).get("mc_buy_gate", {}) or {}
        if not gate_cfg.get("enabled", True):
            return True, "MC 게이트 비활성"
        mc = self._compute_mc_stats(df)
        self._last_mc_stats = mc
        from zusik.analysis.bot_money_helpers import mc_buy_gate_decision
        # 모멘텀/추세 선별 뒤 동일 후보를 MC 55%로 다시 전부 거르는 이중 문턱을 제거한다.
        # 스크리너 자체가 MC일 때만 엄격 문턱을 유지하고, 그 외 방식은 극단적 음의 기대값과
        # 꼬리위험만 차단하는 안전망으로 사용한다.
        method = str((self.config.get("screening", {}) or {}).get(
            "method", "momentum"
        )).strip().lower()
        strict = method == "monte_carlo"
        p_key = "strict_min_p_profit" if strict else "min_p_profit"
        v_key = "strict_min_var95" if strict else "min_var95"
        min_p = float(gate_cfg.get(p_key, 0.55 if strict else 0.40))
        min_var = float(gate_cfg.get(v_key, -0.15 if strict else -0.18))
        ok, reason = mc_buy_gate_decision(mc, min_p_profit=min_p, min_var95=min_var)
        if ok and mc:
            logger.debug("MC OK %s: P(profit>0)=%.0f%% (하한 %.0f%%), "
                         "VaR95=%+.1f%% (하한 %+.0f%%), %.0fms",
                         name, mc["p_profit"] * 100, min_p * 100,
                         mc["var95"] * 100, min_var * 100, mc.get("elapsed_ms", 0))
        return ok, reason

    def _apply_hysteresis(self, code: str, signal: str, conf: float) -> tuple[str, str]:
        """직전 결정과 반대 방향이면 임계 강화. 진동 차단.

        같은 종목 6시간 안에 BUY ↔ SELL이 뒤집히면, 새 신호 confidence가
        70% 미만일 때 hold로 강제 변환. (RIOT 4/29 케이스: 1시간 사이 5번 뒤집힘)
        """
        import time
        prev = self._signal_history.get(code)
        now = time.time()
        # 새 결정 기록 (signal 적용 여부와 무관 — 다음 비교 기준)
        self._signal_history[code] = (now, signal, float(conf or 0))
        if not prev:
            return signal, ""
        prev_time, prev_sig, _ = prev
        if now - prev_time > self.SIGNAL_HYSTERESIS_HOURS * 3600:
            return signal, ""
        buy_set = {"buy", "long_term_buy"}
        reversal = (
            (prev_sig in buy_set and signal == "sell")
            or (prev_sig == "sell" and signal in buy_set)
        )
        if reversal and (conf or 0) < self.SIGNAL_HYSTERESIS_MIN_CONF:
            elapsed_min = (now - prev_time) / 60
            return "hold", (
                f"신호 반전 차단 ({prev_sig.upper()}→{signal.upper()}, "
                f"conf {(conf or 0)*100:.0f}% < {self.SIGNAL_HYSTERESIS_MIN_CONF*100:.0f}%, "
                f"{elapsed_min:.0f}분 만에 뒤집힘)"
            )
        return signal, ""

    def _check_long_term_limit(self, invest_amount: int) -> bool:
        try:
            balance = self.client.get_balance()
        except Exception:
            return False
        total_asset = balance["cash"] + balance["total_eval"]
        if total_asset <= 0:
            return False
        current = self.tracker.get_long_term_total_cost()
        limit = total_asset * self.long_term_ratio
        if current + invest_amount > limit:
            logger.warning("장기투자 한도 초과: %s + %s > %s",
                           f"{current:,}", f"{invest_amount:,}", f"{limit:,}")
            return False
        return True

    def _update_equity_curve(self, deposit_today: int = 0) -> dict | None:
        """잔고 전체 집계해 equity_curve에 일일 스냅샷 기록.

        post_market_report + 일일 목표 도달 시에서 호출. 같은 날 재호출 시 갱신.
        """
        try:
            bal = self.client.get_balance()
            us_bal = self.client.get_us_balance()
            fx = self.client.get_usd_krw_rate()
            kr_cash = int(bal.get("cash", 0))
            kr_eval = int(bal.get("total_eval", 0))
            us_cash_usd = float(us_bal.get("cash_usd", 0))
            us_cash_krw = int(us_cash_usd * fx)
            # us_eval_usd: kis_client가 output1에서 합산한 보유 평가. 폴백으로 직접 합산.
            us_eval_usd = float(us_bal.get("us_eval_usd", 0)) or sum(
                h.get("qty", 0) * h.get("current_price", 0)
                for h in us_bal.get("holdings", []))
            # 한투 API 원화환산 값을 우선 사용 — 한투 앱과 equity curve 일치
            us_eval_krw = us_bal.get("total_eval_krw", 0) or int(us_eval_usd * fx)

            # 미실현 평가차익 계산 (KR + US 원화환산)
            kr_unrealized = sum((h.get("current_price", 0) - h.get("avg_price", 0))
                                * h.get("qty", 0) for h in bal.get("holdings", []))
            us_unrealized_usd = sum((h.get("current_price", 0) - h.get("avg_price", 0))
                                     * h.get("qty", 0) for h in us_bal.get("holdings", []))
            unrealized_krw = int(kr_unrealized + us_unrealized_usd * fx)

            realized = self.tracker.get_realized_pnl_today().get("realized_pnl", 0)
            snap = self.tracker.record_equity_snapshot(
                kr_cash, kr_eval, us_cash_krw, us_eval_krw,
                deposit_today=deposit_today, realized_today=realized,
                fx_rate=fx, us_cash_usd=us_cash_usd,
                us_eval_usd=us_eval_usd, unrealized_krw=unrealized_krw,
                holdings_unrealized_krw=unrealized_krw,  # 보유평가손익 = effective dd 기준
            )
            logger.info("equity snapshot: total %s원 · dd %.2f%% · max %s원",
                        f"{snap['total_equity']:,}",
                        snap['drawdown_pct'], f"{snap['max_equity']:,}")
            return snap
        except Exception:
            logger.debug("equity snapshot 실패", exc_info=True)
            return None

    def _adaptive_params(self) -> dict:
        """누적 손익률 + drawdown 기반 적응형 파라미터 (10분 캐시).

        config.yaml:adaptive.states 룰을 위에서부터 평가, 첫 매칭된 상태의 dict 반환.
        반환 키: correlation, same_sector, cap, rsi_exit_min, rsi_exit_profit_min, name.
        """
        import time as _time
        cfg = self.config.get("adaptive", {})
        if not cfg.get("enabled", True):
            return {}
        now = _time.time()
        ts, cached = getattr(self, "_adapt_cache", (0.0, None))
        if cached is not None and now - ts < 600:
            return cached

        # 시그널 계산: effective 값 사용 — 미국 T+2 미결제로 부풀려진
        # 가짜 dd/pnl이 crisis 상태를 잘못 트리거해 cap 6%·섹터1로 묶던 문제 해결).
        try:
            dd = float(self.tracker.get_effective_drawdown())
        except Exception:
            dd = 0.0
        try:
            pnl_pct = float(self.tracker.get_effective_pnl_pct())
        except Exception:
            pnl_pct = 0.0

        def _eval(trigger: str) -> bool:
            # eval() 제거 — 비교식 문법 파서로 안전 평가 (모듈 상단 eval_adaptive_trigger).
            return eval_adaptive_trigger(trigger, dd, pnl_pct)

        states = cfg.get("states", []) or []
        chosen = None
        for st in states:
            if _eval(st.get("trigger", "")):
                chosen = st
                break
        if chosen is None:
            chosen = {}
        params = {
            "name": chosen.get("name", "default"),
            "correlation": float(chosen.get("correlation", 0.9)),
            "same_sector": int(chosen.get("same_sector", 2)),
            "cap": float(chosen.get("cap", self.config.get("invest_ratio_max", 0.10))),
            "whitelist_cap": float(chosen.get("whitelist_cap", chosen.get("cap", 0.10))),
            "rsi_exit_min": float(chosen.get("rsi_exit_min", 80)),
            "rsi_exit_profit_min": float(chosen.get("rsi_exit_profit_min", 0.03)),
        }

        #: 장 분석 기반 익절 조절 — 강세장은 익절을 늦춰 추세를 살리고,
        # 약세장은 빨리 이익을 확정한다. bull/bear regime score(로컬·10분캐시·Claude無)로
        # rsi_exit_min·rsi_exit_profit_min을 가감. 한국/미국 상승장에 우량주를 너무 일찍
        # 던지던 문제(삼성/하이닉스 미탑승)의 보완책.
        pt_cfg = self.config.get("profit_taking", {}) or {}
        if pt_cfg.get("regime_adaptive", True):
            try:
                bull = float(self._bullish_regime_score())
                bear = float(self._bearish_regime_score())
            except Exception:
                bull = bear = 0.0
            tilt = bull - bear  # -1(약세) ~ +1(강세)
            rsi_tilt = float(pt_cfg.get("rsi_tilt", 8.0))
            profit_tilt = float(pt_cfg.get("profit_tilt", 0.03))
            rsi_lo, rsi_hi = 70.0, 92.0
            prof_lo, prof_hi = 0.01, 0.15
            params["rsi_exit_min"] = max(rsi_lo, min(rsi_hi,
                params["rsi_exit_min"] + tilt * rsi_tilt))
            params["rsi_exit_profit_min"] = max(prof_lo, min(prof_hi,
                params["rsi_exit_profit_min"] + tilt * profit_tilt))
            params["regime_tilt"] = round(tilt, 2)

        logger.info("adaptive 상태: %s (dd=%.2f%%, pnl=%.2f%%, tilt=%+.2f) → corr=%.2f sect=%d cap=%.2f wl_cap=%.2f rsi_exit=%.0f/%.1f%%",
                    params["name"], dd, pnl_pct, params.get("regime_tilt", 0.0),
                    params["correlation"], params["same_sector"], params["cap"],
                    params["whitelist_cap"], params["rsi_exit_min"],
                    params["rsi_exit_profit_min"] * 100)
        self._adapt_cache = (now, params)
        return params

    @staticmethod
    def _build_reward_context(market_condition: str, is_inverse: bool,
                              sell_pattern: str = "", breakout_bias: bool = False) -> str:
        parts = [market_condition or "peace"]
        parts.append("inverse" if is_inverse else "long")
        if sell_pattern:
            parts.append(sell_pattern)
        elif breakout_bias:
            parts.append("breakout")
        else:
            parts.append("default")
        return ":".join(parts)

    def _bullish_regime_score(self) -> float:
        """상승 국면 점수 (0~1). 지수 프록시 ETF 양의 모멘텀.

        bear 점수가 음의 모멘텀에 반응한다면, bull은 양의 모멘텀에 반응.
        지수 ETF만 사용 (보유 종목은 노이즈) — 시장 흐름 자체를 본다.
        10분 캐시.
        """
        import time as _time
        now = _time.time()
        ts, cached = getattr(self, "_bull_cache", (0.0, 0.0))
        if now - ts < 600 and ts > 0:
            return cached

        from zusik.analysis.indicators import momentum_score

        index_scores: list[float] = []
        for code, _name in self._INDEX_PROXIES_KR:
            try:
                df = self.client.get_ohlcv(code)
                if df is not None and len(df) >= 20:
                    index_scores.append(momentum_score(df))
            except Exception:
                continue
        for ticker, exchange in self._INDEX_PROXIES_US:
            try:
                df = self.client.get_us_ohlcv(ticker, exchange=exchange)
                if df is not None and len(df) >= 20:
                    index_scores.append(momentum_score(df))
            except Exception:
                continue

        if not index_scores:
            self._bull_cache = (now, 0.0)
            return 0.0

        avg_m = sum(index_scores) / len(index_scores)
        score = max(0.0, min(1.0, avg_m))
        self._bull_cache = (now, score)
        logger.info("bullish_regime_score=%.2f (index_n=%d)", score, len(index_scores))
        return score

    def _is_whitelist(self, symbol: str) -> bool:
        """whitelist (KR code or US ticker) 여부."""
        if not symbol:
            return False
        screen_cfg = self.config.get("screening", {})
        wl_kr = {w.get("code", "") for w in (screen_cfg.get("whitelist_kr", []) or [])}
        wl_us = {w.get("ticker", "") for w in (screen_cfg.get("whitelist_us", []) or [])}
        return symbol in wl_kr or symbol in wl_us

    def _core_hold_through(self, symbol: str) -> bool:
        """핵심(whitelist) 종목을 이벤트성 급락(중간 폭)에 던지지 않고 홀드할지 (config 토글).

        공장사고 등 '일시적 이벤트' 급락을 crash_instant/quick_loss/slow_bleed가
        펀더멘털 출혈로 오인해 바닥 투매(예: 한화에어로 -5.6% 매도 후 회복)하는 문제 방지.
        깊은 붕괴(-15%↓)·crash_from_high(-20%)·펀더멘털 위험(분식/상폐, LLM 확인)은 여전히 매도.
        """
        if not (self.config.get("position", {}) or {}).get("whitelist_crash_exempt", True):
            return False
        return self._is_whitelist(symbol) and not self._is_inverse(symbol)

    def _learned_hold_floor(self) -> float:
        """비핵심 pullback hold floor 를 sell_timing 사후데이터로 자가 보정(10분 캐시).

        손실측 자가학습: floor 가 게이트하는 손실 컷(crash_instant/slow_bleed/quick_loss)이
        조기였는지(홀드가 우월) 정당했는지(보호)를 데이터로 측정해 floor 를 심화/완화한다.
        '수익률 자동' 학습 루프를 손실측까지 확장한 지점(승자 청산만 학습하던 한계 보완).

        하드스톱(-15%)·deep_collapse 는 floor 밖이라 영향 없음(자본보호 불변). 데이터/토글이
        없거나 표본 부족이면 config 기본값으로 폴백 — 동작 변화 없음(안전한 무동작).
        """
        risk = (self.config.get("risk", {}) or {})
        default = float(risk.get("pullback_hold_floor", -0.09))
        if not risk.get("loss_learning_enabled", True):
            return default
        cached = getattr(self, "_hold_floor_cache", None)
        now = time.time()
        if cached and now - cached[0] < 600:
            return cached[1]
        floor = default
        try:
            from zusik.analysis.loss_learning import learn_hold_floor
            with open(paths.data_path("sell_timing.json"), encoding="utf-8") as f:
                by_pattern = (json.load(f) or {}).get("by_pattern", {})
            res = learn_hold_floor(
                by_pattern, default=default,
                cap=float(risk.get("loss_learning_floor_cap", -0.13)),
                shallow=float(risk.get("loss_learning_floor_shallow", -0.07)))
            floor = float(res["floor"])
            if abs(floor - default) >= 0.005:
                logger.info("손실측 자가학습 floor: %.1f%% (기본 %.1f%%, n=%d, %s)",
                            floor * 100, default * 100, res["n"], res["reason"])
        except FileNotFoundError:
            floor = default
        except Exception as e:
            logger.debug("손실 학습 floor 폴백(기본 사용): %s", e)
            floor = default
        # 안전 하한: 학습값이 어떤 경우에도 하드스톱(-15%)을 잠식하지 못하게 못박는다.
        floor = max(-0.14, min(-0.05, floor))
        self._hold_floor_cache = (now, floor)
        return floor

    def _learned_inverse_quick_profit(self) -> float:
        """인버스 빠른익절 임계(quick_profit_pct)를 inverse_take 사후데이터로 자가 보정(10분 캐시).

        고정 1.5% 대신, 실제 인버스 익절이 너무 빨랐는지(더 갔음)/적정이었는지(되돌림)를
        sell_timing 데이터로 측정해 임계를 올리거나 내린다. 데이터/토글이 없거나 표본 부족이면
        config 기본값으로 폴백(동작 변화 없음). config: inverse.learning_enabled(기본 on),
        inverse.quick_profit_min/max(클램프). 반환은 소수(0.015 = 1.5%)."""
        inv = (self.config.get("inverse", {}) or {})
        default = float(inv.get("quick_profit_pct", 1.5)) / 100.0
        if default <= 0 or not inv.get("learning_enabled", True):
            return default
        cached = getattr(self, "_inv_qp_cache", None)
        now = time.time()
        if cached and now - cached[0] < 600:
            return cached[1]
        th = default
        try:
            from zusik.analysis.loss_learning import learn_inverse_quick_profit
            with open(paths.data_path("sell_timing.json"), encoding="utf-8") as f:
                by_pattern = (json.load(f) or {}).get("by_pattern", {})
            res = learn_inverse_quick_profit(
                by_pattern, default=default,
                min_th=float(inv.get("quick_profit_min", 0.005)),
                max_th=float(inv.get("quick_profit_max", 0.035)))
            th = float(res["threshold"])
            if abs(th - default) >= 0.002:
                logger.info("인버스 빠른익절 자가학습: %.1f%% (기본 %.1f%%, n=%d, %s)",
                            th * 100, default * 100, res["n"], res["reason"])
        except FileNotFoundError:
            th = default
        except Exception as e:
            logger.debug("인버스 익절 학습 폴백(기본 사용): %s", e)
            th = default
        th = max(0.003, min(0.05, th))   # 런어웨이 방지 안전 클램프
        self._inv_qp_cache = (now, th)
        return th

    def _hold_through_loss(self, code: str, profit_rate: float, *,
                           deep_collapse: bool = False) -> bool:
        """조기손절 억제 — 정상 pullback 구간은 매도 보류(KR/US 공통).

        KIS 기록 근거(반사실 백테스트): crash_instant/slow_bleed/rotate = 0% 승률(0/28, -828k).
        같은 종목을 컷 대신 보유했으면 +424k(5봉)~+2.64M(구간 최고가). 즉 조기 컷이 최대 손실원이고
        양 시장 공통이다(NTAP -99k 등 US도 0% 승률). 자본 보호는 하드스톱(-15%)·트레일링·
        RSI 과매수 익절(100% 승률, +1.13M)이 담당.

        - deep_collapse(crash_from_high/-15%↓·펀더멘털 위험)=True → 무조건 매도.
        - 핵심(whitelist): pr > -15%면 홀드.
        - 그 외: pr > floor면 홀드. floor 는 _learned_hold_floor()가 손실 사후데이터로 자가 보정
          (config risk.pullback_hold_floor 기본 -9% → 조기컷이 많으면 더 깊게, 정당컷이면 얕게).

        profit_rate: 소수 표기(-0.05=-5%). 반환 True=매도 보류(홀드).
        """
        if deep_collapse:
            return False
        floor = -0.15 if self._core_hold_through(code) else self._learned_hold_floor()
        return profit_rate > floor

    def _whitelist_core_shares(self, symbol: str) -> int:
        """whitelist 항목의 종목별 코어 목표 주수 (config core_shares). 없으면 0."""
        screen = self.config.get("screening", {}) or {}
        for w in (screen.get("whitelist_kr", []) or []) + (screen.get("whitelist_us", []) or []):
            if w.get("code") == symbol or w.get("ticker") == symbol:
                try:
                    return int(w.get("core_shares", 0) or 0)
                except Exception:
                    return 0
        return 0

    def _maybe_core_topup_kr(self, code: str, name: str, price: float,
                             held_qty: int, intraday_change: float,
                             profit_rate: float = 0.0) -> bool:
        """KR 핵심(whitelist) 코어 타깃 탑업.

        보유 가치가 conviction 하한(목표) 미만이면 목표까지 '단번에' 추가 매수한다.
        pyramid(+수익시만) 경로를 우회하므로 hold 신호·미보유·부분보유 모두에서 동작.
        삼성이 1주씩만, 하이닉스가 0주이던 문제를 한 번에 해결. 매수 시 True 반환.

        가드: whitelist + 비인버스 + 비방어 + 비하락장(bear<0.5) + 비과열(<max_intraday).
        """
        pos_cfg = self.config.get("position", {}) or {}
        if not pos_cfg.get("whitelist_core_entry", True):
            return False
        if not self._is_whitelist(code) or self._is_inverse(code):
            return False
        # churn 루프 방지: 매도 직후 재진입 차단(reentry block)을 코어 패스도 존중.
        # 본전보호/익절 매도 → 30분, 급락/출혈 매도 → 24h 동안 재매수 금지. 이게 없어서
        # '본전보호 매도 → 코어 패스 즉시 재매수 → 또 매도' 루프가 발생했다(현대차 등).
        blocked, br = self._is_reentry_blocked(code)
        if blocked:
            logger.info("핵심주 %s 코어 보류 — %s (churn 방지)", name, br)
            return False
        if getattr(self, "_defensive_mode", False):
            return False
        try:
            if self._bearish_regime_score() >= 0.5:
                logger.info("핵심주 %s 코어 보류 — 하락국면(bear≥0.5)", name)
                return False
        except Exception:
            return False
        if intraday_change >= float(pos_cfg.get("whitelist_core_max_intraday", 0.08)):
            logger.info("핵심주 %s 코어 타깃 보류 — 과열(장중 %+.1f%%)", name, intraday_change * 100)
            return False
        if price <= 0:
            return False
        try:
            bal = self.client.get_balance()
            cash = float(bal.get("cash", 0) or 0)
            base_asset = cash + float(bal.get("total_eval", 0) or 0)
        except Exception:
            return False
        if cash < price:
            logger.info("핵심주 %s 코어 보류 — 현금 %s < 1주 %s", name, f"{int(cash):,}", f"{int(price):,}")
            return False

        target = self._whitelist_min_invest(code, base_asset, cash, price, profit_rate)
        held_val = float(held_qty) * price
        gap = target - held_val
        if gap < price:  # 이미 목표 도달(또는 1주 미만 갭) → 추가 안 함
            logger.debug("핵심주 %s 코어 목표 도달 — 보유 %s ≥ 목표 %s",
                         name, f"{int(held_val):,}", f"{int(target):,}")
            return False
        qty = int(min(gap, cash) / price)
        if qty < 1:
            return False
        if not self.order_guard.can_order(code, "buy"):
            return False

        logger.info("핵심주 코어 타깃 탑업: %s 보유 %s < 목표 %s → %d주 매수 (장중 %+.1f%%)",
                    name, f"{int(held_val):,}", f"{int(target):,}", qty, intraday_change * 100)
        try:
            res = self._submit_kr_market_buy(code, qty, int(price))
        except Exception as e:
            logger.warning("핵심주 코어 탑업 매수 실패 %s: %s", name, str(e)[:120])
            return False
        if not res or not res.get("success"):
            return False
        qty = self._submitted_order_qty(res, qty)
        if qty <= 0:
            logger.warning("핵심주 코어 탑업 성공 응답에 실제 수량 없음: %s", name)
            return False
        self.positions.record_buy(code, name, qty, int(price))
        self.tracker.record_buy(code, name, qty, int(price), False, "핵심주 코어 타깃 매수")
        self.order_guard.record_order(code, "buy", qty, int(price), res.get("order_no", ""))
        if self.discord:
            try:
                self.discord.notify_trade("buy", name, code, qty, int(price),
                                          reason="핵심주 코어 타깃 매수")
            except Exception:
                pass
        return True

    def _core_entry_pass_kr(self):
        """whitelist 핵심주를 Claude 분석 대기 없이 코어 타깃까지 매수 (로컬 패스).

        tick(1분)·run_kr 양쪽에서 호출 → 느린 순차 분석/한 사이클 시세 실패에
        막혀 분산 종목이 안 사지던 문제 해결(매 분 재시도). 동시 실행은 busy 플래그로 차단.
        KR 장중에만 의미. get_current_price만 쓰는 가벼운 패스."""
        pos_cfg = self.config.get("position", {}) or {}
        if not pos_cfg.get("whitelist_core_entry", True):
            return
        if getattr(self, "_core_pass_busy", False):
            return
        try:
            if not self.client.is_market_open():
                return
        except Exception:
            return
        wl = (self.config.get("screening", {}) or {}).get("whitelist_kr", []) or []
        if not wl:
            return
        self._core_pass_busy = True
        try:
            try:
                holdings = self.client.get_balance().get("holdings", [])
                held = {h.get("code"): h.get("qty", 0) for h in holdings}
                # 물타기용 손실률(분율) — 보유 평가손익. 없으면 0.
                held_pr = {h.get("code"): (h.get("profit_rate", 0) or 0) / 100 for h in holdings}
            except Exception:
                return
            for w in wl:
                code = w.get("code")
                if not code:
                    continue
                name = w.get("name", code)
                try:
                    pi = self.client.get_current_price(code)
                    price = float(pi.get("price", 0) or 0)
                    intraday = float(pi.get("change_rate", 0) or 0) / 100
                except Exception as e:
                    logger.info("코어 패스 %s: 시세 조회 실패 → 다음 사이클 재시도 (%s)", name, str(e)[:60])
                    continue
                if price <= 0:
                    logger.info("코어 패스 %s: 가격 0 → 스킵", name)
                    continue
                try:
                    self._maybe_core_topup_kr(code, name, price, held.get(code, 0), intraday,
                                              profit_rate=held_pr.get(code, 0.0))
                except Exception:
                    logger.debug("핵심주 코어 패스 오류 %s", code, exc_info=True)
        finally:
            self._core_pass_busy = False

    @staticmethod
    def _market_for_code(code: str) -> str:
        """종목코드로 시장 추정. KRX는 6자리(전부 숫자, 또는 우선주/ETN 신형식의 끝 1자리 영문)."""
        c = str(code or "")
        return "KR" if (len(c) == 6 and (c.isdigit() or c[:5].isdigit())) else "US"

    def _load_ai_signal_files(self, fresh_h: float):
        """크로스/데일리 AI 신호 파일을 한 번 로드해 짧게 캐시 (게이트·사이징의 중복 read 제거).

        같은 매수 사이클에서 _pre_market_buy_gate 와 _dynamic_invest_ratio 가 종목마다
        _ai_signal_for 를 호출하므로, 디스크 read 를 종목·호출마다 반복하지 않도록 120s 캐시.
        파일 자신의 ts(생성시각)가 없거나(=손상/부분기록) freshness 를 넘으면 stale 로 폐기(fail-closed).
        """
        import json as _json, time as _time
        now = _time.time()
        cache = getattr(self, "_ai_sig_cache", None)
        if cache and (now - cache[0]) < 120:
            return cache[1], cache[2]

        def _load_fresh(name):
            try:
                path = paths.data_path(name)   # 레포 루트 기준 절대경로 (CWD 무관)
                if not os.path.exists(path):
                    return None
                with open(path, encoding="utf-8") as f:
                    d = _json.load(f)
                ts = float(d.get("ts", 0) or 0)
                if ts == 0 or (now - ts) > fresh_h * 3600:
                    return None  # ts 없음(손상) 또는 만료 → 무시 (fail-closed)
                return d
            except Exception:
                return None

        cross = _load_fresh("cross_signals_kr.json")
        daily = _load_fresh("daily_ai_bias.json")
        self._ai_sig_cache = (now, cross, daily)
        return cross, daily

    def _ai_signal_for(self, market: str, code: str) -> dict:
        """크로스시그널(美→韓 익일) + 데일리 Claude 편향을 종목 단위로 합성해 매매에 반영.

        '유저에게만 보여주던' AI 산출물을 실제 매수 게이트/사이징에 태우는 단일 소스.
        반환: {size_mult, min_floor, floor_relief, block, reason}. 데이터 없거나 만료/비활성이면 중립.
          - size_mult: 사이징 배수 [0.7, 1.3] (`_dynamic_invest_ratio`가 곱함)
          - min_floor: AI 약세 편향이 강제하는 매수 확신도 하한 (게이트)
          - floor_relief: AI 강세 편향이 장전 확신도 하한을 낮추는 폭 (게이트)
          - block: AI 매도 판단 → 신규 매수 차단
        """
        neutral = {"size_mult": 1.0, "min_floor": 0.0, "floor_relief": 0.0,
                   "block": False, "reason": ""}
        if not code:
            return neutral
        try:
            cfg = self.config.get("ai_signals", {}) if isinstance(getattr(self, "config", None), dict) else {}
        except Exception:
            cfg = {}
        if not cfg.get("enabled", True):
            return neutral
        fresh_h = float(cfg.get("freshness_hours", 30) or 30)
        cross, daily = self._load_ai_signal_files(fresh_h)

        size = 1.0
        min_floor = 0.0
        relief = 0.0
        block = False
        reasons = []

        # 1) 크로스시그널 (KR 전용 — 美 종목 변동 → 연동 KR 종목)
        if market == "KR" and cross:
            entry = (cross.get("codes") or {}).get(code)
            if entry:
                bias = entry.get("bias")
                if bias == "buy":
                    b = max(0.0, min(0.15, float(entry.get("boost", 0) or 0)))
                    size *= (1.0 + b)
                    relief = max(relief, b)
                    reasons.append(f"크로스 {entry.get('reason', '')}".strip())
                elif bias == "caution":
                    size *= 0.85
                    reasons.append(f"크로스주의 {entry.get('reason', '')}".strip())

        # 2) 데일리 Claude 편향 (KR/US 종목별) — 효과는 _DAILY_BIAS_EFFECT 단일 테이블에서
        if daily:
            side = daily.get("kr" if market == "KR" else "us") or {}
            bias = str(side.get(code, "")).lower().strip()
            eff = _DAILY_BIAS_EFFECT.get(bias)
            if eff:
                sm, fr, mf, bl = eff
                size *= sm
                relief = max(relief, fr)
                min_floor = max(min_floor, mf)
                block = block or bl
                if sm != 1.0 or bl or mf:
                    reasons.append(f"AI일일:{bias}")

        size = max(0.7, min(1.3, size))
        return {"size_mult": size, "min_floor": min_floor, "floor_relief": relief,
                "block": block, "reason": ", ".join([r for r in reasons if r])}

    def _pre_market_buy_gate(self, market: str, confidence: float,
                             symbol: str = "") -> tuple[bool, str]:
        """장전 sentiment + AI 신호(크로스/데일리) 기반 신규 매수 허용 여부.

        (allow, reason) 반환. 같은 거래일 sentiment 파일이 없거나 오래됐으면 그 레이어는 중립.
        AI 신호는 symbol(종목)이 주어졌을 때만 적용 — 강세는 확신도 하한을 낮추고,
        약세는 하한을 올리거나(min_floor) 매도 판단이면 신규 매수를 차단한다(진입 게이트).
        whitelist도 같은 게이트를 적용한다. 관심종목이라는 이유만으로 시장 위험회피나
        종목별 매도 판단을 우회하면, 데이터 기반 선별을 거치지 않은 강제 진입이 된다.
        """
        import json as _json
        # ── 장전 sentiment 레이어 (없거나 stale이면 중립) ──
        avoid_new_buy = False
        min_conf = 0.0
        stance = ""
        neg_hits = 0
        sent_file = os.path.join("data", f"pre_market_sentiment_{market}.json")
        if os.path.exists(sent_file):
            try:
                with open(sent_file, encoding="utf-8") as f:
                    sent = _json.load(f)
                if sent.get("date") == datetime.now().strftime("%Y-%m-%d"):  # 어제 → 무시
                    avoid_new_buy = bool(sent.get("avoid_new_buy"))
                    min_conf = float(sent.get("min_buy_confidence", 0.60))
                    stance = sent.get("stance", "")
                    neg_hits = sent.get("neg_hits", 0)
            except Exception:
                pass
        # ── AI 신호 레이어 (종목 단위) ──
        ai = self._ai_signal_for(market, symbol) if symbol else None
        ai_reason = ai.get("reason", "") if ai else ""
        if ai and ai.get("block"):
            return False, f"AI 신호 차단: {ai_reason}"
        if avoid_new_buy:
            return False, f"장전 판단 '{stance}' (neg={neg_hits}) → 신규 매수 차단"
        relief = float(ai.get("floor_relief", 0.0)) if ai else 0.0
        ai_floor = float(ai.get("min_floor", 0.0)) if ai else 0.0
        # 장전 sentiment의 min_conf는 그대로 존중(상한 캡 없음) — 0.85 캡이 0.90 같은 높은
        # 요구치를 몰래 낮추던 회귀 차단. AI는 강세면 낮추고(relief) 약세면 올린다(ai_floor).
        eff_floor = max(0.0, max(min_conf - relief, ai_floor))
        if eff_floor > 0 and confidence < eff_floor:
            extra = f" + {ai_reason}" if ai_reason else ""
            return False, (f"장전 판단 '{stance or 'neutral'}' — 확신도 {confidence*100:.0f}% "
                           f"< 요구 {eff_floor*100:.0f}%{extra}")
        note = f"장전 '{stance or 'neutral'}' — 확신도 {confidence*100:.0f}% ≥ {eff_floor*100:.0f}%"
        if ai_reason:
            note += f" | AI: {ai_reason}"
        return True, note

    def _update_portfolio_info(self):
        """현재 보유 현황 캐시 갱신 — 장 시작/마감 시 호출."""
        try:
            bal = self.client.get_balance()
            kr_holdings = bal.get("holdings", [])
            kr_cash = bal["cash"]

            us_bal = self.client.get_us_balance()
            us_holdings = us_bal.get("holdings", [])

            info_parts = []

            # KR
            if kr_holdings:
                items = [f"{h['name']} {h['qty']}주({h['profit_rate']:+.1f}%)" for h in kr_holdings]
                info_parts.append(f"KR 보유: {', '.join(items)}")
            else:
                info_parts.append("KR 보유 없음")
            info_parts.append(f"KR 현금: {kr_cash:,}원")

            # US
            if us_holdings:
                items = [f"{h['name']} {h['qty']}주({h['profit_rate']:+.1f}%)" for h in us_holdings]
                info_parts.append(f"US 보유: {', '.join(items)}")
            else:
                info_parts.append("US 보유 없음")

            self._cached_portfolio_info = " | ".join(info_parts)
            logger.info("포트폴리오 갱신: %s", self._cached_portfolio_info)
        except Exception:
            self._cached_portfolio_info = ""

    def _reconcile_external_trades(self):
        """수동 매매(MTS/HTS) 감지 → 실현손익에 반영. 알림도 발송.

        장 휴장 시에는 잔고 API가 빈 holdings를 반환할 수 있어 "전체 매도"로
        오판하고 가짜 수동 매도를 기록하는 버그가 있었음.
        따라서 해당 시장이 열렸을 때만 실행한다. 수동 매매는 어차피 장중에만
        가능하므로 기능적 손실 없음.
        """
        try:
            if self.client.is_market_open():
                kr_bal = self.client.get_balance()
                kr_holdings = kr_bal.get("holdings", [])
                n_kr = self.tracker.reconcile_external_trades(kr_holdings, market="KR", fx_rate=1.0)
                if n_kr > 0 and self.discord:
                    self.discord.notify_error(f"수동 매도 {n_kr}건 감지(KR) → 실현손익 반영됨")
        except Exception:
            logger.warning("KR 외부 매매 동기화 실패", exc_info=True)
        try:
            if self.client.is_us_market_open():
                us_bal = self.client.get_us_balance()
                us_holdings = us_bal.get("holdings", [])
                fx = self.client.get_usd_krw_rate()
                n_us = self.tracker.reconcile_external_trades(us_holdings, market="US", fx_rate=fx)
                if n_us > 0 and self.discord:
                    self.discord.notify_error(f"수동 매도 {n_us}건 감지(US) → 실현손익 반영됨")
        except Exception:
            logger.warning("US 외부 매매 동기화 실패", exc_info=True)

    def _auto_detect_deposits(self, current_total: int, eq: dict):
        """자동 입금/출금 감지 → data/total_deposits.json 갱신.

        원리:
          total_equity는 trade(buy/sell)로 변하지 않음 (cash↔eval 이동만).
          → 실현 P&L과 시세 변동(허용치)을 빼고 남은 변화는 외부 자금 유입.

        config.deposits.auto_detect 토글 추가. 기본 false (비활성).
          반복 오탐 사례:
            - hantu_tot이 KR 보유 평가 누락 → 직접합산과 ±44k 오차 → 출입금으로 오인
            - 매도 직후 KIS 스냅샷 transient lag → 자산 일시 감소 → 출금으로 오인
          사용자 의도와 무관하게 manual_total_krw를 변동시켜 수익률 보고가 왜곡됨.
          입출금은 사용자 수동 입력만 신뢰 (config에서 명시적으로 켜야 동작).
        """
        cfg_dep = self.config.get("deposits", {}) if isinstance(self.config, dict) else {}
        if not cfg_dep.get("auto_detect", False):
            return
        try:
            STATE_FILE = os.path.join("data", "deposit_sync_state.json")
            DEPOSITS_FILE = os.path.join("data", "total_deposits.json")
            os.makedirs("data", exist_ok=True)

            current_realized = 0
            try:
                rinfo = self.tracker.get_realized_pnl_total()
                current_realized = int(rinfo.get("total_realized_pnl", 0))
            except Exception:
                pass

            holdings_value = max(int(eq.get("kr_eval", 0))
                                  + int(eq.get("us_eval_usd", 0)
                                        * eq.get("us_total_krw", 0)
                                        / max(eq.get("us_total_usd", 1), 1)),
                                  0)
            # 시세 변동 허용치: 보유 가치의 5%, 최소 5,000원
            allowance = max(int(holdings_value * 0.05), 5000)

            prev_state = None
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, encoding="utf-8") as f:
                        prev_state = json.load(f)
                except Exception:
                    prev_state = None

            now_iso = datetime.now().isoformat()
            new_state = {
                "ts": now_iso,
                "prev_total": current_total,
                "prev_realized_total": current_realized,
            }

            if prev_state is None:
                # 최초 — baseline 저장만, 추정 X
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_state, f, ensure_ascii=False, indent=2)
                return

            prev_total = int(prev_state.get("prev_total", current_total))
            prev_realized = int(prev_state.get("prev_realized_total", current_realized))
            delta_total = current_total - prev_total
            realized_delta = current_realized - prev_realized
            inferred = delta_total - realized_delta

            if abs(inferred) >= allowance:
                # 입금/출금 추정
                deposits_data = {"manual_total_krw": 0, "history": []}
                if os.path.exists(DEPOSITS_FILE):
                    try:
                        with open(DEPOSITS_FILE, encoding="utf-8") as f:
                            deposits_data = json.load(f)
                    except Exception:
                        pass

                new_total = int(deposits_data.get("manual_total_krw", 0)) + inferred
                deposits_data["manual_total_krw"] = max(new_total, 0)
                deposits_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                deposits_data.setdefault("history", []).append({
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "amount": int(inferred),
                    "market": "AUTO",
                    "note": (f"자동 감지 (자산 {prev_total:,}→{current_total:,}, "
                             f"실현 +{realized_delta:,}, 허용치 {allowance:,})"),
                })
                with open(DEPOSITS_FILE, "w", encoding="utf-8") as f:
                    json.dump(deposits_data, f, ensure_ascii=False, indent=2)

                kind = "입금" if inferred > 0 else "출금"
                logger.info("자동 %s 감지: %+d원 → 누적 입금 %s원",
                             kind, inferred, f"{deposits_data['manual_total_krw']:,}")
                if self.discord:
                    try:
                        self.discord.notify_info(
                            f"자동 {kind} 감지: {inferred:+,}원 "
                            f"(자산 {prev_total:,}→{current_total:,}, "
                            f"실현 +{realized_delta:,}) "
                            f"→ 누적 입금 {deposits_data['manual_total_krw']:,}원"
                        )
                    except Exception:
                        pass

            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(new_state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug("자동 입금 감지 오류: %s", str(e)[:120])

    def _hourly_equity_sync(self):
        """주기적 자산 동기화(5분 cooldown — 단축).

        이전 1시간 → 5분: 사용자가 입금/출금 즉시 반영. 동기화 비용은
        KIS API 4호출(get_balance, get_us_balance, fx, present-balance)로 가벼움.
        """
        last = getattr(self, "_last_equity_sync", None)
        now = datetime.now()
        if last and (now - last).total_seconds() < 300:
            return
        self._last_equity_sync = now
        import threading
        threading.Thread(target=self._sync_equity_now, daemon=True).start()

    def _sync_equity_now(self):
        """실시간 자산 동기화."""
        try:
            from zusik.analysis.bot_money_helpers import compute_total_equity, compute_pnl_vs_deposit
            b = self.client.get_balance()
            us = self.client.get_us_balance()
            fx = self.client.get_usd_krw_rate()

            eq = compute_total_equity(b, us, fx)
            total = eq["total"]

            # 자동 입금/출금 감지 → total_deposits.json 자동 갱신.
            # total_equity는 매매로 변하지 않고 시세 변동·실현손익·외부 자금 유입에만 변함.
            # delta_total - realized_delta - eval_swing_허용치 가 임계 초과면 입금/출금 추정.
            self._auto_detect_deposits(total, eq)

            # 보유 평가손익 (결제 타이밍 무관) — effective drawdown 산출용.
            kr_unrl = sum((h.get("current_price", 0) - h.get("avg_price", 0)) * h.get("qty", 0)
                          for h in b.get("holdings", []))
            us_unrl_usd = sum((h.get("current_price", 0) - h.get("avg_price", 0)) * h.get("qty", 0)
                              for h in us.get("holdings", []))
            holdings_unrl_krw = int(kr_unrl + us_unrl_usd * fx)

            snap = self.tracker.record_equity_snapshot(
                kr_cash=eq["kr_settled"], kr_eval=eq["kr_eval"],
                us_cash_krw=int(eq["us_cash_usd"] * fx),
                us_eval_krw=int(eq["us_eval_usd"] * fx),
                fx_rate=fx,
                us_cash_usd=eq["us_cash_usd"], us_eval_usd=eq["us_eval_usd"],
                #: unrealized_krw에 us_pending_usd(미정산 매도대금)를
                # 넣어 실효 순수익이 +8.4M로 폭증했다. 실제 미실현 평가차익(holdings_unrl_krw)으로 교체.
                unrealized_krw=holdings_unrl_krw,
                total_override=total,  #: net pending 포함 정확한 total로 max/dd 계산
                holdings_unrealized_krw=holdings_unrl_krw,  #: effective dd 기준
            )
            snap["us_pending_usd"] = eq["us_pending_usd"]
            snap["kr_settled"] = eq["kr_settled"]

            # 입금 대비 P&L (drawdown 부풀림 보정) — 자동 감지로 갱신된 값 사용
            deposits = (self.tracker.get_total_deposits()
                        if hasattr(self.tracker, "get_total_deposits") else 100_000)
            pnl = compute_pnl_vs_deposit(total, deposits)
            snap["deposits_total"] = deposits
            snap["pnl_vs_deposit"] = pnl["pnl"]
            snap["pnl_pct"] = pnl["pnl_pct"]

            self._write_equity_snapshot_with_dd(snap, total)

            # 의사결정 기준인 '실효(effective)' 손익/드로우다운을 헤드라인으로 — 결제(T+2) 타이밍에
            # 무관하게 안정적. 표시용 raw total/pnl 은 괄호로 보조 (결제 구간엔 일시 출렁일 수 있음).
            eff_eq = snap.get("effective_equity", total)
            eff_pnl_pct = snap.get("effective_pnl_pct", pnl["pnl_pct"])
            eff_dd = snap.get("effective_drawdown_pct", snap.get("drawdown_pct", 0))
            eff_pnl = int(eff_eq - deposits)
            logger.info(
                "자산 동기화: 실효손익 %+s원 (%+.2f%%) dd %+.1f%% [의사결정 기준] | "
                "표시 total %s원 (%+.2f%%, dd %+.1f%%) | 입금 %s원",
                f"{eff_pnl:+,}", eff_pnl_pct, eff_dd,
                f"{total:,}", pnl["pnl_pct"], snap.get("drawdown_pct", 0),
                f"{deposits:,}")
        except Exception:
            logger.exception("equity 동기화 오류")

    def _write_equity_snapshot_with_dd(self, snap: dict, total: int):
        """equity_curve.json 갱신 + max/drawdown 재계산.

        `_hourly_equity_sync`에서 직접 net pending까지 포함한 total로 호출.
        post_market_report 경로의 `_update_equity_curve(deposit_today=…)`(line 3080)는
        tracker를 거쳐 별도 snap 생성 — 두 메서드가 동시에 존재해야 한다.
        """
        from zusik.storage.portfolio_tracker import EQUITY_CURVE_FILE, _load_json, _save_json
        if not os.path.exists(EQUITY_CURVE_FILE):
            return
        # 로드/저장 모두 tracker 공용 경로로 — 이전의 raw open("w") 직접 쓰기가
        # tracker의 원자 저장과 경합해 파일을 찢던(Extra data 파손) 원인.
        curve = _load_json(EQUITY_CURVE_FILE)
        if not isinstance(curve, list):
            return
        max_eq = max(*(d.get("max_equity", 0) for d in curve), total) if curve else total
        snap["max_equity"] = max_eq
        snap["drawdown_pct"] = round((total - max_eq) / max_eq * 100, 2) if max_eq > 0 else 0
        if curve and curve[-1]["date"] == snap["date"]:
            curve[-1] = snap
        else:
            curve.append(snap)
        _save_json(EQUITY_CURVE_FILE, curve)
    def _submit_kr_market_buy(self, code: str, qty: int, reference_price: int) -> dict:
        """브로커별 인터페이스를 보존하며 KR 시장가 매수를 전송한다.

        KIS는 종목별 ``nrcvb_buy_qty`` 사전조회에 기준가격이 필요하므로 전달한다.
        Toss/Fake 등 기존 브로커는 2-인자 계약을 그대로 사용한다.
        """
        if hasattr(self.client, "get_orderable_buy_info"):
            return self.client.buy_market(code, qty, reference_price=reference_price)
        return self.client.buy_market(code, qty)

    @staticmethod
    def _submitted_order_qty(result: dict | None, requested_qty: int) -> int:
        """브로커가 주문가능수량에 맞춰 감액한 실제 전송 수량."""
        if not result or not result.get("success"):
            return 0
        try:
            return int(result.get("submitted_qty", requested_qty) or 0)
        except (TypeError, ValueError):
            return 0
