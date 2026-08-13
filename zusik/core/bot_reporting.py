from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from zusik import paths


logger = logging.getLogger(__name__)


class ReportingMixin:
    """ReportingMixin -- bot.py에서 분리된 관심사 묶음 (TradingBot의 베이스)."""

    def _send_pre_market_report(self, market: str, force: bool = False):
        """장 시작 전 Claude 분석 리포트 → Discord.

        market: "KR" 또는 "US"
        force: True면 파일 가드 무시 (Discord 수동 명령용)
        파일 가드로 하루 1회만 발송 (재시작 후에도 유지).
        """
        #: config.reports.pre_market_enabled=false면 스킵 (Claude Opus 절약)
        if not self.config.get("reports", {}).get("pre_market_enabled", True) and not force:
            return
        from zusik.clients.discord_bot import _discord_bot_ref
        import asyncio, threading

        if not _discord_bot_ref or not _discord_bot_ref._alert_channel:
            return

        # 파일 기반 중복 방지 (재시작 후에도 유지) — force=True면 우회
        today = datetime.now().strftime("%Y-%m-%d")
        guard_file = os.path.join("data", f"last_pre_report_{market}.txt")
        if not force:
            try:
                if os.path.exists(guard_file):
                    with open(guard_file) as f:
                        if f.read().strip() == today:
                            logger.info("%s 장전 리포트 스킵 — 오늘 이미 발송됨", market)
                            return
            except Exception:
                pass

        # 즉시 마킹 (Claude 호출 30초+ 동안 다른 호출 차단)
        try:
            os.makedirs("data", exist_ok=True)
            with open(guard_file, "w") as f:
                f.write(today)
        except Exception:
            pass

        def _generate():
            today_str = datetime.now().strftime("%Y-%m-%d (%a)")
            try:
                from zusik.clients.claude_client import ClaudeClient
                cl = ClaudeClient(prefer_cli=True)

                # 현재 잔고 + 실제 매수 가능한 종목만 추림
                try:
                    bal = self.client.get_balance()
                    kr_cash = bal.get("cash", 0)
                    kr_holdings = bal.get("holdings", [])
                except Exception:
                    bal, kr_cash, kr_holdings = {}, 0, []
                try:
                    us_bal = self.client.get_us_balance()
                    us_cash = us_bal.get("cash_usd", 0.0)             # settled (매수 가능)
                    us_cash_display = us_bal.get("display_cash_usd",  # 한투 앱 일치
                                                  us_cash + us_bal.get("sell_pending_usd", 0))
                    us_pending = us_bal.get("sell_pending_usd", 0.0)
                    us_holdings = us_bal.get("holdings", [])
                except Exception:
                    us_bal = {}
                    us_cash, us_cash_display, us_pending, us_holdings = 0.0, 0.0, 0.0, []

                allocation_note = ""
                if market == "US":
                    try:
                        fx = float(self.client.get_usd_krw_rate() or 0)
                        us_eval_usd = float(us_bal.get("us_eval_usd", 0) or 0)
                        if us_eval_usd <= 0:
                            us_eval_usd = sum(
                                float(h.get("eval_amount", 0) or 0) for h in us_holdings)
                        from zusik.reporting.status_snapshot import (
                            build_allocation_advice, latest_complete_allocation_snapshot,
                            render_allocation_advice,
                        )
                        allocation_row = {
                            "kr_cash": kr_cash,
                            "kr_eval": int(bal.get("total_eval", 0) or 0),
                            "us_cash_krw": int(us_cash * fx),
                            "us_eval_krw": int(us_eval_usd * fx),
                        }
                        # 장 마감 후 KR API가 현금·평가액을 모두 0으로 주는 경우가 있다.
                        # 로컬 포지션에 KR 종목이 남아 있으면 마지막 완전 스냅샷으로 계산한다.
                        tracked = getattr(self.positions, "_positions", {}) or {}
                        has_kr_position = any(
                            str(code).isdigit() and (pos.get("qty", 0) or 0) > 0
                            for code, pos in tracked.items() if isinstance(pos, dict)
                        )
                        if has_kr_position and not (
                                allocation_row["kr_cash"] or allocation_row["kr_eval"]):
                            from zusik.storage.portfolio_tracker import EQUITY_CURVE_FILE, _load_json
                            fallback = latest_complete_allocation_snapshot(
                                _load_json(EQUITY_CURVE_FILE))
                            if fallback:
                                allocation_row = fallback
                        allocation_note = render_allocation_advice(
                            build_allocation_advice(allocation_row, self.config))
                    except Exception:
                        logger.debug("US 자금배분 권고 계산 실패", exc_info=True)

                def _affordable_kr():
                    """미보유 && 현금으로 1주 살 수 있는 KR 종목만 반환."""
                    held = {h["code"] for h in kr_holdings}
                    out = []
                    for s in self.kr_stocks:
                        code = s.get("code")
                        name = s.get("name", code)
                        if code in held:
                            out.append(f"{name}(보유중)")
                            continue
                        try:
                            p = self.client.get_current_price(code).get("price", 0)
                            if p > 0 and p <= kr_cash:
                                out.append(f"{name}(@{p:,}원)")
                        except Exception:
                            pass
                    return out

                def _affordable_us():
                    held = {h["ticker"] for h in us_holdings}
                    out = []
                    for s in self.us_stocks:
                        tk = s.get("ticker")
                        name = s.get("name", tk)
                        exch = s.get("exchange", "NASD")
                        if tk in held:
                            out.append(f"{name}(보유중)")
                            continue
                        try:
                            p = self.client.get_us_current_price(tk, exch).get("price", 0)
                            if p > 0 and p <= us_cash:
                                out.append(f"{name}(@${p:.2f})")
                        except Exception:
                            pass
                    return out

                if market == "KR":
                    affordable = _affordable_kr()
                    held_text = ", ".join(f"{h['name']} {h['qty']}주({h['profit_rate']:+.1f}%)" for h in kr_holdings) or "없음"
                    if not affordable:
                        cand_text = f"(현금 {kr_cash:,}원 부족 — 매수 후보 없음. 보유 종목 매도 타이밍만 분석)"
                    else:
                        cand_text = ", ".join(affordable)
                    prompt = (
                        f"오늘({today_str}) 한국 주식 시장 장전 분석. 웹 검색으로 실제 데이터 조사.\n\n"
                        f"내 계좌: 예수금 {kr_cash:,}원, 보유: {held_text}\n"
                        f"매수 검토 대상(현금으로 살 수 있는 종목만): {cand_text}\n\n"
                        f"1. 어제 미국장 마감 결과와 한국 영향\n"
                        f"2. 코스피/코스닥 전망\n"
                        f"3. 환율, 원자재\n"
                        f"4. 위 매수 검토 대상 중에서만 매수 추천 (못 사는 종목은 언급 금지).\n"
                        f"   보유 종목은 매도/보유 판단.\n"
                        f"5. 오늘 주의할 이슈\n\n"
                        f"Discord 형식(표 금지, 이모지). 한국어.\n"
                        f"예수금 범위 밖의 종목(삼성전자/SK하이닉스 등)은 절대 추천하지 마세요."
                    )
                    title = f"KR 장 시작 전 분석 — {today_str}"
                else:
                    affordable = _affordable_us()
                    held_text = ", ".join(f"{h['name']} {h['qty']}주({h['profit_rate']:+.1f}%)" for h in us_holdings) or "없음"
                    if not affordable:
                        cand_text = f"(즉시 매수 가능 ${us_cash:.2f} 부족 — 매수 후보 없음. 보유 종목 매도 타이밍만 분석)"
                    else:
                        cand_text = ", ".join(affordable)
                    pending_note = f" (미정산 ${us_pending:.2f})" if us_pending > 0.01 else ""
                    prompt = (
                        f"오늘({today_str}) 미국 주식 시장 장전 분석. 웹 검색으로 실제 데이터 조사.\n\n"
                        f"내 계좌: 달러 예수금 ${us_cash_display:.2f}{pending_note} (즉시 매수 가능 ${us_cash:.2f}), 보유: {held_text}\n"
                        f"매수 검토 대상(달러로 살 수 있는 종목만): {cand_text}\n\n"
                        f"1. 오늘 한국장 결과와 미국 영향\n"
                        f"2. S&P500, 나스닥, 다우 전망\n"
                        f"3. VIX, 국채금리\n"
                        f"4. 위 매수 검토 대상 중에서만 매수 추천 (못 사는 종목은 언급 금지).\n"
                        f"   보유 종목은 매도/보유 판단.\n"
                        f"5. 프리마켓 동향 + 실적 발표 예정\n\n"
                        f"Discord 형식(표 금지, 이모지). 한국어.\n"
                        f"달러 예수금 범위 밖의 고가 종목은 절대 추천하지 마세요."
                    )
                    title = f"US 장 시작 전 분석 — {today_str}"

                #: 장전 리포트 tier="hard"(sonnet) → "cheap_web"(agy/codex 우선).
                # 사용자 Claude 쿼터 절감 — web_search는 유지.
                report = cl.message(prompt, use_web_search=True, tier="cheap_web")

                if report and len(report) > 50:
                    if allocation_note:
                        report = f"{report}\n\n💵 {allocation_note}"
                    async def _send():
                        ch = _discord_bot_ref._alert_channel
                        await ch.send(f"**{title}**")
                        chunks = [report[i:i+1900] for i in range(0, len(report), 1900)]
                        for chunk in chunks:
                            await ch.send(chunk)
                    asyncio.run_coroutine_threadsafe(_send(), _discord_bot_ref.loop)
                    logger.info("%s 장전 리포트 전송 (%d자)", market, len(report))

                    # 리포트 텍스트에서 sentiment 추출 → 매수 gate에 반영
                    try:
                        sent = self._analyze_pre_market_sentiment(report, market)
                        sent_file = os.path.join("data", f"pre_market_sentiment_{market}.json")
                        import json as _json
                        with open(sent_file, "w", encoding="utf-8") as f:
                            _json.dump(sent, f, ensure_ascii=False, indent=2)
                        logger.info("%s 장전 sentiment: stance=%s avoid_buy=%s min_conf=%.2f",
                                    market, sent["stance"], sent["avoid_new_buy"],
                                    sent["min_buy_confidence"])
                    except Exception:
                        logger.debug("%s sentiment 저장 실패", market, exc_info=True)

                    # 리포트(뉴스)에서 이벤트 감지 → 활성 수혜 섹터 갱신 (이벤트 로테이션 선별에 반영)
                    try:
                        self._refresh_active_event_sectors(report)
                    except Exception:
                        logger.debug("%s 이벤트 섹터 감지 실패", market, exc_info=True)
            except Exception:
                logger.warning("%s 장전 리포트 실패", market, exc_info=True)

        threading.Thread(target=_generate, daemon=True).start()

    def pre_market_alert(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._pre_market_notified == today:
            return

        # 파일 기반 중복 방지
        notify_file = os.path.join("data", "last_pre_alert.txt")
        try:
            if os.path.exists(notify_file):
                with open(notify_file) as f:
                    if f.read().strip() == today:
                        self._pre_market_notified = today
                        return
        except Exception:
            pass

        if not self.client.is_weekday():
            return

        # ── 즉시 플래그 + 파일 저장 (진행 중 다른 tick이 재진입 못 하게) ──
        self._pre_market_notified = today
        try:
            os.makedirs("data", exist_ok=True)
            with open(notify_file, "w") as f:
                f.write(today)
        except Exception:
            pass

        logger.info("──── KR 장 시작 전 알림 ────")

        # 매일 아침 강제 종목 재선별 (당일 시장 상황 반영)
        import threading
        threading.Thread(target=lambda: self._refresh_stocks(force=True), daemon=True).start()

        # KR 장전 분석 리포트 (플래그 이미 저장됨 → 중복 호출 차단됨)
        self._send_pre_market_report("KR")

    def post_market_report(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._post_market_notified == today:
            return

        # 파일 기반 중복 방지 (재시작해도 유지)
        notify_file = os.path.join("data", "last_report.txt")
        try:
            if os.path.exists(notify_file):
                with open(notify_file) as f:
                    if f.read().strip() == today:
                        self._post_market_notified = today
                        return
        except Exception:
            pass

        if not self.client.is_weekday():
            return

        # ── 즉시 플래그 + 파일 저장 (진행 중 다른 tick이 재진입 못 하게) ──
        self._post_market_notified = today
        try:
            os.makedirs("data", exist_ok=True)
            with open(notify_file, "w") as f:
                f.write(today)
        except Exception:
            pass

        logger.info("──── 장 마감 리포트 ────")
        try:
            balance = self.client.get_balance()
        except Exception:
            logger.exception("잔고 조회 실패")
            return

        realized_today = self.tracker.get_realized_pnl_today()
        realized_total = self.tracker.get_realized_pnl_total()
        self.tracker.get_trades_today()
        self.tracker.get_long_term_holdings()
        risk_status = self.risk.get_status()

        total_asset = balance["cash"] + balance["total_eval"]
        perf = self.reward.get_performance_report()

        # ── 훈련 시스템: 주간 점검 + 월간 평가 ──
        self.trainer.set_start_asset(total_asset)

        weekly = self.trainer.weekly_checkpoint(total_asset, realized_total["total_realized_pnl"])
        if weekly:
            logger.info("주간 점검: %s", weekly["message"])
            if weekly["status"] == "behind" and self.discord:
                self.discord.notify_error(f"주간 점검: {weekly['message']} — 전략 재검토 필요")

        monthly = self.trainer.monthly_evaluation(
            total_asset, realized_total["total_realized_pnl"],
            analyst_standings=self.strategy.analyst.get_analyst_standings() if self.use_claude else None,
        )
        if monthly:
            logger.info("월간 평가: %s (%+d점)", monthly["result"].upper(), monthly["score"])
            if self.discord:
                merit_emoji = "+" if monthly["score"] > 0 else ""
                self.discord._send(embeds=[{
                    "title": f"월간 성과 평가 — {'메리트' if monthly['score'] > 0 else '디메리트'}",
                    "color": 0x2ECC71 if monthly["score"] > 0 else 0xE74C3C,
                    "fields": [
                        {"name": "실제 수익률", "value": f"{monthly['actual_rate']:+.2%}", "inline": True},
                        {"name": "목표 수익률", "value": f"{monthly['target_rate']:+.2%}", "inline": True},
                        {"name": "점수", "value": f"{merit_emoji}{monthly['score']}점 (누적 {monthly['cumulative_merit']})", "inline": True},
                        {"name": "조정 사항", "value": "\n".join(monthly["adjustments"]), "inline": False},
                    ],
                }])

        logger.info("=== 일일 리포트 ===")
        logger.info("실현손익(확정): 오늘 %s원 | 누적 %s원",
                     f"{realized_today['realized_pnl']:+,}",
                     f"{realized_total['total_realized_pnl']:+,}")
        logger.info("미실현(미확정): %s원", f"{balance['total_profit']:+,}")
        if risk_status["emergency_hold"]:
            logger.warning("긴급 홀딩 중: %s", risk_status["emergency_reason"])
        logger.info("현재 전략: %s", self.strategy.name)

        # 보상 엔진 성과
        if perf["total_trades"] > 0:
            logger.info("── 학습 성과 ──")
            streak = perf["streak"]
            logger.info("  총 %d건 거래 | %s",
                        perf["total_trades"],
                        f"{streak}연속 수익 중" if streak > 0 else
                        f"{abs(streak)}연속 손실 중" if streak < 0 else "연속 없음")
            for sname, sdata in perf["strategies"].items():
                if sdata["trades"] > 0:
                    logger.info("  전략 '%s': 승률 %.0f%% | 가중치 %.2f | %s원",
                                sname, sdata["win_rate"], sdata["weight"], f"{sdata['total_pnl']:+,}")
            wc = perf["winning_conditions"]
            if wc.get("win_count", 0) > 0:
                logger.info("  승리 패턴: %s", wc["summary"])

        # 3인 애널리스트 성적표
        if self.use_claude:
            standings = self.strategy.analyst.get_analyst_standings()
            if standings:
                logger.info("── 애널리스트 경쟁 성적 ──")
                for name_kr, s in standings.items():
                    logger.info("  [%s] %d전 %d승 (%.0f%%) — 가중치 %.2f",
                                name_kr, s["total"], s["correct"], s["accuracy"], s["weight"])

        if self.use_claude:
            self.strategy.analyst.get_analyst_standings()

        # 아레나 토글 (의사결정 미사용 모니터링 — 기본 OFF로 낭비 차단)
        arena_on = bool((self.config.get("arena", {}) or {}).get("enabled", False))

        # Discord Bot으로 리포트 전송 (Webhook 대신)
        from zusik.clients.discord_bot import _discord_bot_ref
        if _discord_bot_ref and _discord_bot_ref._alert_channel:
            import asyncio as _asyncio
            report_text = self.commander._handle_status()
            arena_text = self.arena.get_report() if arena_on else ""

            async def _send_report():
                ch = _discord_bot_ref._alert_channel
                await ch.send(f"**장 마감 리포트 — {datetime.now().strftime('%Y-%m-%d')}**")
                chunks = [report_text[i:i+1900] for i in range(0, len(report_text), 1900)]
                for chunk in chunks:
                    await ch.send(f"```\n{chunk}\n```")
                if arena_text:
                    await ch.send(f"```\n{arena_text}\n```")
            _asyncio.run_coroutine_threadsafe(_send_report(), _discord_bot_ref.loop)

        # 아레나 주간 평가 (비활성 시 스킵)
        weekly = self.arena.weekly_evaluation() if arena_on else None
        if weekly and self.discord:
            from zusik.clients.discord_bot import _discord_bot_ref, asyncio
            if _discord_bot_ref and _discord_bot_ref._alert_channel:
                report = self.arena.get_report()
                leader = weekly["leader"]["agent"]
                loser = weekly["loser"]["agent"]
                async def _send_arena():
                    ch = _discord_bot_ref._alert_channel
                    await ch.send(
                        f"**아레나 주간 평가**\n"
                        f"1등: {leader} → 실전 투입\n"
                        f"꼴찌: {loser} → 리셋\n```\n{report}\n```"
                    )
                asyncio.run_coroutine_threadsafe(_send_arena(), _discord_bot_ref.loop)

        # Claude 장문 분석 리포트 → Discord Bot으로 전송
        # (플래그/파일은 메서드 시작 시 이미 저장되어 재진입 차단됨)
        self._send_claude_daily_report()

        # EOD 매도 패턴 리포트 — 승리 패턴 시각화로 자가 학습 피드백
        self._send_eod_pattern_report()
        # 수정 효과 추적 — baseline(hold-through·모델선택·whitelist 재설계) 전/후 신규거래 비교
        self._send_fix_effect_report()

        # 일일 equity 스냅샷 + 월간 리포트 (매월 1일 1회)
        self._update_equity_curve()
        self._generate_monthly_html_report()   # 매달 마지막 날 — HTML 요약 저장
        self._send_monthly_report()             # 매월 1일 — 지난 달 Discord 요약

    def _send_claude_daily_report(self):
        """Claude가 작성하는 장문 일일 분석 리포트 → Discord 전송."""
        #: config.reports.daily_enabled=false면 스킵 (Claude Opus 절약)
        if not self.config.get("reports", {}).get("daily_enabled", True):
            return
        from zusik.clients.discord_bot import _discord_bot_ref
        import asyncio

        if not _discord_bot_ref or not _discord_bot_ref._alert_channel:
            return

        # 한도 체크 — 리포트 1건이 Claude 1건 소비
        if not self.cost.can_call("claude"):
            logger.info("일일 리포트 스킵 — Claude 한도 도달")
            return

        try:
            # 보유 종목 + 잔고 + 매수 가능 종목 수집
            holdings_info = ""
            kr_cash = 0
            us_cash_usd = 0.0
            try:
                bal = self.client.get_balance()
                kr_cash = bal.get("cash", 0)
                for h in bal.get("holdings", []):
                    holdings_info += f"KR {h['name']}({h['code']}): {h['qty']}주, 평단가 {h['avg_price']:,}원, 현재가 {h['current_price']:,}원 ({h['profit_rate']:+.1f}%)\n"
            except Exception:
                pass
            try:
                us = self.client.get_us_balance()
                us_cash_usd = us.get("cash_usd", 0.0)  # settled (매수 가능)
                us_cash_display = us.get("display_cash_usd",
                                          us_cash_usd + us.get("sell_pending_usd", 0))
                us_pending_usd = us.get("sell_pending_usd", 0.0)
                for h in us.get("holdings", []):
                    holdings_info += f"US {h['name']}({h['ticker']}): {h['qty']}주, 평단가 ${h['avg_price']:.2f}, 현재가 ${h['current_price']:.2f} ({h['profit_rate']:+.1f}%)\n"
            except Exception:
                pass

            # 현금으로 살 수 있는 종목만 후보로 제시
            kr_candidates = []
            try:
                held_kr = {h["code"] for h in (bal.get("holdings", []) if bal else [])}
                for s in self.kr_stocks:
                    if s.get("code") in held_kr:
                        continue
                    try:
                        p = self.client.get_current_price(s["code"]).get("price", 0)
                        if 0 < p <= kr_cash:
                            kr_candidates.append(f"{s.get('name', s['code'])}(@{p:,}원)")
                    except Exception:
                        pass
            except Exception:
                pass
            us_candidates = []
            try:
                held_us = {h["ticker"] for h in (us.get("holdings", []) if us else [])}
                for s in self.us_stocks:
                    tk = s.get("ticker")
                    if tk in held_us:
                        continue
                    try:
                        p = self.client.get_us_current_price(tk, s.get("exchange", "NASD")).get("price", 0)
                        if 0 < p <= us_cash_usd:
                            us_candidates.append(f"{s.get('name', tk)}(@${p:.2f})")
                    except Exception:
                        pass
            except Exception:
                pass

            kr_cand_text = ", ".join(kr_candidates) or f"(예수금 {kr_cash:,}원으로 살 수 있는 종목 없음)"
            us_cand_text = ", ".join(us_candidates) or f"(달러 ${us_cash_usd:.2f}로 살 수 있는 종목 없음)"
            today = datetime.now().strftime("%Y-%m-%d")

            prompt = f"""오늘({today}) 장 마감 기준 시장 분석 리포트를 작성해. 웹 검색으로 실제 데이터를 조사해.

일일 시장 분석 리포트

내 계좌 상황:
  KR 예수금: {kr_cash:,}원
  US 예수금: ${us_cash_display:.2f}{f' (미정산 ${us_pending_usd:.2f} 포함, 즉시 매수 가능 ${us_cash_usd:.2f})' if us_pending_usd > 0.01 else ''}
{holdings_info or '  보유 종목: 없음'}

매수 검토 대상 (예수금 범위 내):
  KR: {kr_cand_text}
  US: {us_cand_text}

1. 글로벌 시장: S&P500, 나스닥, 코스피, 환율, VIX, 비트코인 (수치 포함)
2. 주요 이슈: 전쟁, 금리, 실적 등
3. 보유 종목 매도/보유 판단
4. 내일 전략: **위에 명시된 매수 검토 대상 중에서만** 매수 추천
   (예수금 밖의 고가 종목 추천 금지 — 삼성전자/SK하이닉스 등)
5. 리스크 요인과 대응

중요: Discord에서 읽을 거라 아래 규칙을 반드시 지켜:
- 마크다운 표(|---|) 절대 사용 금지
- 대신 이모지와 들여쓰기로 정리
- 구분선은 ──────── 사용
한국어로 구체적 수치와 함께 전문가 수준으로 작성.

마지막에, 사람이 읽는 본문과 별개로 기계 판독용 JSON 한 줄을 정확히 아래 형식으로 출력해
(코드블록·설명·다른 텍스트 없이 마지막 줄에 단독으로):
BIAS_JSON={{"kr": {{"종목코드": "buy|hold|reduce|sell"}}, "us": {{"티커": "buy|hold|reduce|sell"}}}}
규칙: 위 '보유 종목'과 '매수 검토 대상'에 등장한 KR 종목코드(6자리)·US 티커만 포함.
확신이 약하면 "hold". 신규 진입 매력적이면 "buy", 비중 축소 권고면 "reduce", 청산 권고면 "sell"."""

            from zusik.clients.claude_client import ClaudeClient
            claude = ClaudeClient(prefer_cli=True)
            #: 장후 일일 리포트 tier="hard"(sonnet) → "cheap_web"(agy/codex 우선).
            # 사용자 Claude 쿼터 절감 — web_search는 유지.
            report = claude.message(prompt, use_web_search=True, tier="cheap_web")

            # 기계 판독용 편향(BIAS_JSON) 추출 → 매매 반영(data/daily_ai_bias.json), 본문에선 제거.
            # '유저에게만 보여주던' 일일 분석을 매수 게이트/사이징에 태우는 경로 (_ai_signal_for 소비).
            try:
                report, bias = self._parse_daily_bias(report)
                if bias and (bias.get("kr") or bias.get("us")):
                    paths.write_json_atomic(
                        paths.data_path("daily_ai_bias.json"),
                        {"ts": time.time(), "date": today,
                         "kr": bias.get("kr", {}), "us": bias.get("us", {})})
                    logger.info("AI 일일 편향 저장: KR %d, US %d종목 → 매수 게이트/사이징 반영",
                                len(bias.get("kr", {})), len(bias.get("us", {})))
            except Exception:
                logger.debug("AI 일일 편향 파싱 실패", exc_info=True)

            # API 오류 응답(Usage credits, 1M context, rate limit 등)이 본문으로 들어오면
            # Discord에 그대로 전송하지 않고 logger.warning으로만 남김.
            error_markers = ("Usage credits required", "1M context",
                             "claude.ai/settings/usage", "rate limit",
                             "API Error")
            if report and any(m.lower() in report.lower() for m in error_markers):
                logger.warning("일일 리포트 API 오류 응답 감지, Discord 전송 차단: %s",
                               report[:200])
                report = ""

            if report and len(report) > 100:
                async def _send():
                    ch = _discord_bot_ref._alert_channel
                    await ch.send(f"**AI 일일 시장 분석 리포트 — {today}**")
                    chunks = [report[i:i+1900] for i in range(0, len(report), 1900)]
                    for chunk in chunks:
                        await ch.send(chunk)

                asyncio.run_coroutine_threadsafe(_send(), _discord_bot_ref.loop)
                logger.info("Claude 일일 리포트 Discord 전송 (%d자)", len(report))
        except Exception:
            logger.warning("Claude 일일 리포트 생성 실패", exc_info=True)

    @staticmethod
    def _parse_daily_bias(report_text: str):
        """리포트 끝의 `BIAS_JSON={...}` 마커를 파싱.

        Returns: (사용자용 본문, bias dict|None). bias = {"kr": {code: verdict}, "us": {...}}.
        verdict는 허용 어휘(_DAILY_BIAS_EFFECT 키)만 통과·소문자 정규화. 파싱 실패/없으면 None.

        `raw_decode`로 마커 뒤 '첫 JSON 값'만 파싱한다 — 모델이 BIAS_JSON 뒤에 군더더기
        텍스트(닫는 중괄호 포함)를 덧붙여도 끝까지 greedy 매칭해 통째로 잃지 않게 한다.
        마커는 rfind(마지막 출현)로 — 본문에서 형식을 설명하며 언급한 경우와 구분.
        """
        import json as _json
        from zusik.core.bot_helpers import DAILY_BIAS_VERDICTS
        if not report_text:
            return report_text, None
        marker = "BIAS_JSON="
        i = report_text.rfind(marker)
        if i == -1:
            return report_text, None
        clean = report_text[:i].rstrip()
        rest = report_text[i + len(marker):].lstrip()
        try:
            obj, _end = _json.JSONDecoder().raw_decode(rest)
        except Exception:
            return clean, None
        if not isinstance(obj, dict):
            return clean, None
        out = {"kr": {}, "us": {}}
        for mk in ("kr", "us"):
            side = obj.get(mk) or {}
            if isinstance(side, dict):
                for k, v in side.items():
                    vv = str(v).lower().strip()
                    if vv in DAILY_BIAS_VERDICTS:
                        out[mk][str(k)] = vv
        return clean, out

    def _send_fix_effect_report(self) -> None:
        """수정 효과 추적 리포트 (post_market 1회) — baseline 전/후 신규거래 비교 → 로그 + Discord.

        조기손절 비중·손익이 줄고 실현/승률이 개선되는지 자동 추적 (자가 검증 피드백).
        """
        try:
            from zusik.analysis.fix_effect import format_report
            baseline = (self.config.get("reports", {}) or {}).get("fix_baseline_date", "2026-06-03")
            eff = self.tracker.get_fix_effect(baseline)
            text = format_report(eff)
            logger.info("수정 효과 추적:\n%s", text)
            from zusik.clients.discord_bot import _discord_bot_ref
            import asyncio
            if _discord_bot_ref and getattr(_discord_bot_ref, "_alert_channel", None):
                async def _send():
                    await _discord_bot_ref._alert_channel.send(f"```\n{text}\n```")
                asyncio.run_coroutine_threadsafe(_send(), _discord_bot_ref.loop)
        except Exception:
            logger.debug("수정 효과 리포트 실패", exc_info=True)

    def _send_eod_pattern_report(self, market: str = "KR") -> None:
        """장 마감 후 '오늘 + 해당 시장' 매도의 sell_pattern 집계 Discord 발송.

        KR은 `post_market_report`(한국장 마감), US는 tick의 US post_market 분기에서 호출.
        - '오늘'(date==today) + 해당 market 매도만 집계 — days=1은 cutoff off-by-one으로
          어제 매도까지, market 무필터라 타 시장 매도까지 섞여 일일 리포트를 오염시켰음
          (예: 한국장 리포트에 간밤 미국장 MSFT 손절 -103k 혼입).
        - 실효 수익 분해(계좌 전체)는 중복 방지 위해 KR 마감 때 1회만.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        market_label = "미국장" if market == "US" else "한국장"
        try:
            if market == "US":
                # US 정규장은 KST 자정을 넘김(22:30~05:00/06:00) → '오늘 날짜'로 거르면 자정
                # 이전(전날 저녁) 체결을 놓친다. 직전 세션을 시각 윈도(11h)로 정확히 포착.
                # 11h는 세션(≈6.5h)+여유를 덮고, 직전 세션(≈24h 전)은 안전하게 제외.
                from datetime import timedelta
                since = (datetime.now() - timedelta(hours=11)).isoformat()
                stats = self.tracker.get_pattern_stats(since=since, market="US")
            else:
                stats = self.tracker.get_pattern_stats(on_date=today, market="KR")
        except Exception:
            logger.debug("EOD 패턴 통계 실패", exc_info=True)
            stats = {}
        if stats:
            total_pnl = sum(s["pnl_sum"] for s in stats.values())
            logger.info(f"EOD 패턴 리포트({market_label}): 합계 {total_pnl:+,}원, 패턴 {len(stats)}종")
            if self.discord:
                try:
                    self.discord.notify_pattern_report(today, stats, total_pnl, market=market_label)
                except Exception:
                    logger.debug("EOD 패턴 알림 실패", exc_info=True)

        if market != "KR":
            return  # 실효 수익 분해는 계좌 전체 — KR 마감 때 1회만

        # 실효 수익 분해 (실현 / 미실현 / 환율효과 / 명목증가)
        try:
            from zusik.storage.portfolio_tracker import _load_json, EQUITY_CURVE_FILE
            curve = _load_json(EQUITY_CURVE_FILE) or []
            latest = max(curve, key=lambda c: c.get("date", ""), default=None)
            if latest:
                total_equity = int(latest.get("total_equity", 0))
                unrealized = int(latest.get("unrealized_krw", 0))
                summary = self.tracker.get_effective_pnl_summary(total_equity, unrealized)
                logger.info(
                    f"EOD 실효 수익: realized {summary['realized_total']:+,} · "
                    f"unrealized {summary['unrealized_krw']:+,} · "
                    f"apparent {summary['apparent_gain']:+,} · "
                    f"fx {summary['fx_and_other_effect']:+,}"
                )
                if self.discord:
                    try:
                        self.discord.notify_effective_pnl(today, summary)
                    except Exception:
                        logger.debug("EOD 실효 수익 알림 실패", exc_info=True)
        except Exception:
            logger.debug("실효 수익 분해 실패", exc_info=True)

    def _llm_configured_providers(self) -> list:
        """설치·활성된 LLM provider 목록(메시지 표기용) — 1회 캐시.

        codex/claude/agy 를 다 쓰는 사람·하나만 쓰는 사람을 구분해, 알림에 '실제 쓰는 것'만
        지목하기 위함(예: agy만 쓰면 'agy 확인'). down(=전부 실패)이면 이들이 곧 실패한 provider.
        """
        if hasattr(self, "_llm_providers_cache"):
            return self._llm_providers_cache
        provs = []
        try:
            import os
            from zusik.clients.claude_client import ClaudeClient
            ai = ClaudeClient(prefer_cli=True)
            if getattr(ai, "_has_claude", False): provs.append("claude")
            if getattr(ai, "_has_codex", False): provs.append("codex")
            if getattr(ai, "_has_agy", False): provs.append("agy")
            if not provs and os.getenv("ANTHROPIC_API_KEY"): provs.append("API")
        except Exception:
            provs = []
        self._llm_providers_cache = provs
        return provs

    def _check_llm_health(self):
        """LLM 전체 불가 시 메신저 통보 — 장중(하루) 1회만, 실제 쓰는 provider만 지목.

        message()가 data/llm_health.json 에 집계한 상태를 읽는다. dedup 은 날짜 기준 파일 영속
        (data/llm_alert.json) 이라 잦은 재시작·플래핑에도 하루 1회만 발송. 끄기: config.ai_providers
        .llm_health_alert=false. 로컬 전략은 계속 매매하므로 '왜 AI 분석이 안 오지'를 알리는 게 목적.
        """
        if not (self.config.get("ai_providers", {}) or {}).get("llm_health_alert", True):
            return
        try:
            from zusik.clients.claude_client import get_llm_health
            h = get_llm_health()
        except Exception:
            return
        status = h.get("status", "ok")
        if not self.discord:
            return
        import json as _json
        from zusik import paths
        sf = paths.data_path("llm_alert.json")
        try:
            with open(sf, encoding="utf-8") as f:
                st = _json.load(f) or {}
        except Exception:
            st = {}
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            if status == "down" and st.get("down_date") != today:
                who = "/".join(self._llm_configured_providers()) or "AI"
                reason = h.get("last_reason", "")
                msg = (f"LLM 작동 불가 — {who} 쿼터·로그인 확인. "
                       f"로컬 전략으로 매매는 지속됩니다.")
                if reason:
                    msg += f"\n사유: {reason}"
                self.discord.notify_error(msg)
                st["down_date"] = today
                paths.write_json_atomic(sf, st)
            elif status == "ok" and st.get("down_date") == today \
                    and st.get("recovered_date") != today:
                self.discord.notify_info("LLM 복구 — AI 분석 정상화")
                st["recovered_date"] = today
                paths.write_json_atomic(sf, st)
        except Exception:
            logger.debug("LLM health 통보 실패", exc_info=True)

    def _write_status_snapshot(self):
        """data/status.json 단일 상태 스냅샷 기록 — CLI(--status)·웹(/api/status) 공용 소스.

        흩어진 상태(effective 자산·종목별 손익·보유·켜진 토글·시장/WS/무결성·최근 결정)를
        한 파일로. 파일 기반(API 0)이라 매 tick 호출 무방. 무예외(실패해도 봇 무영향)."""
        try:
            from datetime import datetime
            from zusik.reporting.status_snapshot import build_status_snapshot
            from zusik import paths
            snap = build_status_snapshot(
                self, generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            paths.write_json_atomic(paths.data_path("status.json"), snap)
            return snap
        except Exception:
            logger.debug("status 스냅샷 기록 실패", exc_info=True)
            return {}

    def _generate_monthly_html_report(self) -> None:
        """매달 마지막 날 post_market 에 이번 달 최종 수익 요약을 HTML 로 저장.

        reports/monthly/{YYYY-MM}.html (자가완결 HTML). 저장 후 메신저로 위치+요약 통지.
        월 1회 dedup. data/ 상태 → 사람이 읽는 산출물 분리.
        """
        from datetime import timedelta
        today = datetime.now()
        if (today + timedelta(days=1)).month == today.month:
            return  # 아직 이달 마지막 날이 아님
        flag_key = f"_monthly_html_generated_{today.strftime('%Y-%m')}"
        if getattr(self, flag_key, False):
            return
        try:
            stats = self.tracker.get_monthly_stats(today.year, today.month)
            if stats.get("days", 0) == 0:
                return
            try:  # 진입 버킷 ROI — Discord 월간 리포트와 동일 축 (이 달 기준)
                stats["entry_buckets"] = self.tracker.get_entry_bucket_stats(
                    month=today.strftime("%Y-%m"))
            except Exception:
                pass
            from zusik.reporting.monthly_html import write_monthly_html
            from zusik import paths
            path = write_monthly_html(stats, paths.reports_path("monthly"),
                                      generated_at=today.strftime("%Y-%m-%d %H:%M"))
            logger.info("월간 HTML 리포트 생성: %s (수익률 %+.2f%%)", path, stats["return_pct"])
            try:  # PDF 변환(백엔드 있으면) — best-effort, 실패해도 HTML 은 유효
                from zusik.reporting.pdf import html_to_pdf
                pdf = html_to_pdf(path, path[:-5] + ".pdf")
                if pdf:
                    logger.info("월간 PDF 리포트 생성: %s", pdf)
            except Exception:
                logger.debug("월간 PDF 변환 실패", exc_info=True)
            if self.discord:
                try:
                    self.discord.notify_monthly_html_ready(stats, path)
                except Exception:
                    logger.debug("월간 HTML 알림 실패", exc_info=True)
        except Exception:
            logger.debug("월간 HTML 생성 실패", exc_info=True)
            return
        setattr(self, flag_key, True)

    def _send_monthly_report(self) -> None:
        """매월 1일에 지난 달 월간 성과를 Discord로 발송.

        tick()에서 day==1 + post_market 타이밍에 1회.
        """
        today = datetime.now()
        if today.day != 1:
            return
        flag_key = f"_monthly_report_sent_{today.strftime('%Y-%m')}"
        if getattr(self, flag_key, False):
            return
        # 지난 달 계산
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month > 1 else today.year - 1
        try:
            stats = self.tracker.get_monthly_stats(prev_year, prev_month)
        except Exception:
            logger.debug("월간 리포트 생성 실패", exc_info=True)
            return
        if stats.get("days", 0) == 0:
            return
        # 진입 버킷 ROI 상시 관측 — 월간 결산과 같은 기간(지난 달)으로 집계해
        # 종목별/패턴별 손익과 원인 분석 축이 어긋나지 않게 한다
        try:
            stats["entry_buckets"] = self.tracker.get_entry_bucket_stats(
                month=f"{prev_year}-{prev_month:02d}")
        except Exception:
            pass

        logger.info("월간 리포트: %s 수익률 %+.2f%% / drawdown %+.2f%%",
                    stats["month"], stats["return_pct"], stats["max_drawdown"])
        if self.discord:
            try:
                self.discord.notify_monthly_report(stats)
            except Exception:
                logger.debug("월간 리포트 알림 실패", exc_info=True)
        setattr(self, flag_key, True)

    @staticmethod
    def _analyze_pre_market_sentiment(report_text: str, market: str) -> dict:
        """장전 리포트 텍스트를 로컬 키워드 점수로 분석 → 매수 gate용 sentiment.

        Claude 2차 호출 없이 간단·안정적으로 작동. 출력:
          stance: "cautious"|"neutral"|"bullish"
          avoid_new_buy: True면 신규 매수 원칙 차단
          min_buy_confidence: 매수 허용 최소 확신도 (0.5~0.85)
          reason: 판정 근거 (로그·디버그용)
        """
        import time as _t
        text = (report_text or "").lower()
        neg_words = [
            "자제", "비추천", "방어", "관망", "회피", "주의", "위험", "경계",
            "하락 지속", "추가 조정", "불확실", "변동성 확대", "리스크",
            "매수 타이밍 아님", "매수 부적절", "매수 삼가", "매수 미루",
            "현금 보유", "현금 확보", "매도 우위", "약세", "베어",
            "cautious", "risk", "avoid", "wait", "defensive", "bearish", "volatile",
        ]
        pos_words = [
            "매수 추천", "진입 유리", "반등", "저가 매수", "기회", "눌림목",
            "지지 확인", "돌파", "상승 전환", "강세", "불리시", "우상향",
            "buy the dip", "bullish", "breakout", "rally",
        ]
        neg_hits = sum(text.count(w) for w in neg_words)
        pos_hits = sum(text.count(w) for w in pos_words)

        # 임계값 추가 완화: 5/14 KR sentiment neg=10/pos=5에서 avoid=True로
        # KR 매매 전면 차단된 케이스. pos 시그널 5개나 있는데 neg 절대값(>=8)만으로 차단되는
        # 게 너무 빡빡. net(neg-pos) 기반으로 전환 + 확신도 게이트는 유지.
        net = neg_hits - pos_hits
        if neg_hits == 0 and pos_hits == 0:
            stance = "neutral"
            avoid = False
            min_conf = 0.55
        else:
            ratio = neg_hits / max(pos_hits, 1)
            if ratio >= 2.0 or (neg_hits >= 5 and pos_hits == 0):
                stance = "cautious"
                # 전면 차단은 진짜 강한 부정만: net(neg-pos) ≥ 15 또는 ratio ≥ 6
                # (이전 neg≥8/ratio≥4는 너무 빡빡해 정상 시황도 차단)
                avoid = net >= 15 or ratio >= 6.0
                min_conf = 0.70  # 차단 안 될 때도 확신도 70%+ 매수만
            elif ratio >= 1.0:
                stance = "cautious"
                avoid = False
                min_conf = 0.55  #: 0.62 → 0.55, 확신도 60% 도달 매수 차단 무한반복 케이스 해소
            elif ratio <= 0.5:
                stance = "bullish"
                avoid = False
                min_conf = 0.45
            else:
                stance = "neutral"
                avoid = False
                min_conf = 0.55

        return {
            "market": market,
            "ts": _t.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "stance": stance,
            "avoid_new_buy": bool(avoid),
            "min_buy_confidence": float(min_conf),
            "neg_hits": neg_hits,
            "pos_hits": pos_hits,
            "reason": f"neg={neg_hits} pos={pos_hits} stance={stance}",
        }

    def _format_error_alert(self, scope: str, target: str, exc: Exception) -> str:
        """Discord 오류 알림 포맷터.

        `KR 원익IPS 오류`처럼 맥락 없는 메시지 대신 예외 타입/메시지/마지막 프레임을
        포함해 즉시 원인 추적 가능하게 함(사용자 요청).
        동일 (scope, target, err_type) 조합은 10분 이내 중복 발송 억제.
        """
        import time as _t, traceback as _tb
        err_type = type(exc).__name__
        err_msg = str(exc).strip()[:300] or "(메시지 없음)"

        # 마지막 traceback 프레임 한 줄 (우리 코드 위치가 대부분)
        frames = _tb.format_exception(type(exc), exc, exc.__traceback__)
        last_frame = ""
        for line in reversed("".join(frames).splitlines()):
            line = line.strip()
            if line.startswith("File ") and "/zusik/" in line:
                last_frame = line
                break
        if not last_frame and frames:
            last_frame = frames[-1].strip()[:200]

        # 10분 dedup
        now = _t.time()
        key = (scope, target, err_type)
        cache = getattr(self, "_err_alert_cache", {})
        last_sent = cache.get(key, 0)
        if now - last_sent < 600:
            return ""  # 빈 문자열 → notify_error 호출 측에서 자체 skip 용도
        cache[key] = now
        self._err_alert_cache = cache

        lines = [f"{scope} {target} 오류", f"`{err_type}`: {err_msg}"]
        if last_frame:
            lines.append(f"```{last_frame}```")
        return "\n".join(lines)

    def backtest_report(self):
        if not self.use_adaptive:
            logger.info("adaptive 전략에서만 지원합니다.")
            return
        for stock in self.stocks:
            df = self.client.get_ohlcv(stock["code"], period=self.period)
            if df is not None and not df.empty:
                self.strategy.select_best_strategy(df)

    def claude_report(self):
        if not self.use_claude:
            logger.info("claude 전략에서만 지원합니다.")
            return
        for stock in self.stocks:
            code = stock["code"]
            name = stock.get("name") or self._get_name(code)
            logger.info("▶ %s(%s)...", name, code)
            df = self.client.get_ohlcv(code, period=self.period)
            if df is None or df.empty:
                continue
            self.strategy.set_stock(code, name)
            self.strategy.analyze(df)
            a = self.strategy.get_last_analysis()
            if a:
                label = {"buy": "단기매수", "long_term_buy": "장기매수", "sell": "매도", "hold": "관망"}.get(a["signal"], a["signal"])
                logger.info("  %s | 확신도 %.0f%%", label, a["confidence"] * 100)
                logger.info("  %s", a["reasoning"])
                if a.get("long_term_reason"):
                    logger.info("  장기사유: %s", a["long_term_reason"])
            time.sleep(1)
