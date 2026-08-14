from __future__ import annotations
"""Discord 명령 인터페이스.

Discord 채널에서 봇에게 명령을 보내면 실행.
Webhook으로 보내고, 주기적으로 메시지를 확인하는 방식 대신
간단한 파일 기반 명령 큐를 사용.

사용법:
  1. Discord에서 명령 입력 (또는 파일에 직접 작성)
  2. 봇이 매 틱마다 명령 파일 확인
  3. 명령 실행 후 Discord로 결과 응답

지원 명령:
  /종목 추가 KR 005930 삼성전자
  /종목 추가 US SOFI SoFi NASD
  /종목 제거 KR 005930
  /종목 제거 US SOFI
  /종목 목록
  /상태
  /모드 aggressive
  /매수 KR 005930 10000
  /매도 KR 005930
  /리포트
  /긴급홀딩
  /홀딩해제
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

CMD_FILE = os.path.join("data", "commands.json")


def _load_commands() -> list:
    if os.path.exists(CMD_FILE):
        try:
            with open(CMD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_commands(cmds: list):
    os.makedirs("data", exist_ok=True)
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmds, f, ensure_ascii=False, indent=2)


def queue_command(cmd: str):
    """명령을 큐에 추가 (외부에서 호출)."""
    cmds = _load_commands()
    cmds.append({
        "cmd": cmd.strip(),
        "timestamp": datetime.now().isoformat(),
        "processed": False,
    })
    _save_commands(cmds)


class DiscordCommander:
    """Discord 명령 처리기."""

    def __init__(self, bot):
        """bot: TradingBot 인스턴스."""
        self.bot = bot

    def process_pending(self):
        """대기 중인 명령 처리."""
        cmds = _load_commands()
        if not cmds:
            return

        new_cmds = []
        for c in cmds:
            if not c.get("processed"):
                cmd = c.get("cmd", "")
                
                # 1. 특수 명령: 알림 전송 (watchdog 등에서 사용)
                if cmd == "watchdog_alert" and c.get("message"):
                    if self.bot.discord:
                        self.bot.discord._send(content=c["message"])
                    c["processed"] = True
                
                # 2. 일반 명령 실행
                else:
                    result = self._execute(cmd)
                    c["processed"] = True
                    c["result"] = result
                    # Discord로 결과 응답
                    if self.bot.discord and result:
                        self.bot.discord._send(content=f"**명령 결과:**\n{result}")
            
            new_cmds.append(c)

        # 최근 50개만 유지
        _save_commands(new_cmds[-50:])

    def _execute(self, cmd: str) -> str:
        """명령 파싱 + 실행."""
        parts = cmd.strip().split()
        if not parts:
            return "빈 명령"

        action = parts[0].lower().replace("/", "")

        try:
            if action == "종목" and len(parts) >= 2:
                return self._handle_stock(parts[1:])
            elif action == "상태":
                return self._handle_status()
            elif action == "모드" and len(parts) >= 2:
                return self._handle_mode(parts[1])
            elif action == "매수" and len(parts) >= 3:
                return self._handle_buy(parts[1:])
            elif action == "매도" and len(parts) >= 2:
                return self._handle_sell(parts[1:])
            elif action == "리포트":
                return self._handle_report()
            elif action == "긴급홀딩":
                return self._handle_emergency_hold()
            elif action == "홀딩해제":
                return self._handle_emergency_release()
            elif action == "손실해제":
                return self._handle_loss_release()
            elif action == "성과" or action == "수익":
                return self._handle_performance()
            elif action == "헬스" or action == "health":
                return self._handle_health()
            elif action == "점검" or action == "healthcheck":
                return self._handle_healthcheck()
            elif action == "업데이트" or action == "update":
                return self._handle_update()
            elif action == "도움" or action == "help":
                return self._handle_help()
            else:
                return f"알 수 없는 명령: {action}\n/도움 으로 명령어 확인"
        except Exception as e:
            return f"명령 실행 오류: {e}"

    # ── 성과 요약 ──

    def _handle_performance(self) -> str:
        """누적 실효 성과 + 매도 패턴/타이밍 + 선택 alpha 요약 (Discord/Telegram 텍스트).

        매도 타이밍/선택 alpha 는 운영자 스크립트(sell_timing_review·selection_alpha_review)가
        만든 data/*.json 캐시가 있을 때만 표시(없으면 안내). 표(|---|)는 디스코드에서 깨지므로 사용 금지.
        """
        try:
            from zusik.reporting.results_html import build_results_summary
            s = build_results_summary(self.bot.tracker)
        except Exception as e:
            return f"성과 집계 실패: {e}"
        L = ["=== 누적 성과 (실효 기준) ==="]
        L.append(f"수익률 {s.get('return_pct', 0):+.2f}% · 실효순익 {s.get('effective_total', 0):+,}원")
        L.append(f"실현 {s.get('realized_total', 0):+,} · 미실현 {s.get('unrealized', 0):+,}")
        L.append(f"승률 {s.get('win_rate', 0)}% ({s.get('wins', 0)}/{s.get('sells', 0)}) · "
                 f"MaxDD {s.get('max_drawdown', 0):+.2f}%")
        pats = (s.get("patterns") or [])[:3]
        if pats:
            L.append("\n매도 패턴 상위(손익):")
            for p in pats:
                L.append(f"  {p.get('pattern')}: {p.get('count')}건 "
                         f"승률 {p.get('win_rate', 0):.0f}% {p.get('pnl_sum', 0):+,}원")
        st = s.get("sell_timing")
        if isinstance(st, dict) and st.get("by_pattern"):
            L.append("\n매도 타이밍(놓친상승/막은하락):")
            for pat, v in list(st["by_pattern"].items())[:3]:
                L.append(f"  {pat}: 놓침 {v.get('avg_missed_upside', 0):+.1f}% / "
                         f"막음 {v.get('avg_avoided_drop', 0):+.1f}% — {v.get('verdict', '')}")
        sel = s.get("selection_alpha")
        if isinstance(sel, dict) and sel.get("alpha"):
            a = sel["alpha"]
            L.append(f"\n종목선택 alpha {a.get('avg_alpha', 0):+.2f}%p (지수 초과율 {a.get('beat_index_rate', 0)}%)")
            for mk, m in (sel.get("by_market") or {}).items():
                L.append(f"  {mk}: alpha {m.get('avg_alpha', 0):+.2f}%p ({m.get('count', 0)}건)")
        if not (isinstance(st, dict) and st.get("by_pattern")) and not (isinstance(sel, dict) and sel.get("alpha")):
            L.append("\n(매도 타이밍·선택 alpha: scripts/sell_timing_review.py · "
                     "selection_alpha_review.py 실행 시 표시)")
        return "\n".join(L)

    # ── 종목 관리 ──

    def _handle_stock(self, args: list) -> str:
        sub = args[0] if args else "목록"

        # 수동 추가/제거는 매 사이클 자동선별(screening)이 kr_stocks/us_stocks 를 통째로 덮어써
        # 즉시 사라진다 → 더 이상 지원하지 않고 현재 자동선별 목록만 보여준다.
        if sub in ("추가", "제거"):
            return ("종목은 이제 자동선별(스크리닝)이 매 사이클 결정합니다 — 수동 추가/제거는 "
                    "다음 선별에 덮어써져 적용되지 않습니다. /종목 으로 현재 목록을 확인하세요.")

        kr = ", ".join(f"{s.get('name', s.get('code'))}({s.get('code', '')})" for s in self.bot.kr_stocks)
        us = ", ".join(f"{s.get('name', s.get('ticker'))}({s.get('ticker', '')})" for s in self.bot.us_stocks)
        return f"자동선별 감시 종목\nKR: {kr or '없음'}\nUS: {us or '없음'}"

    # ── 상태 ──

    def _handle_status(self) -> str:
        try:
            realized = self.bot.tracker.get_realized_pnl_total()
            temp = self.bot.cost.get_market_temperature()
            mode = self.bot._active_mode

            usage = self.bot.cost.get_today_usage()
            lines = ["=== 전체 상태 ==="]
            lines.append(f"모드: {mode.upper()} | 시장온도: {temp['temperature']} (캐시 {temp['cache_ttl']}분)")
            lines.append(f"실현손익(확정): {realized['total_realized_pnl']:+,}원")
            # 실효 손익률 (입금+실현+평가 기준, 결제 타이밍 무관) — 매매 게이트가 보는 진짜 값.
            # 총자산 표시는 미국 T+2 미결제로 일시 왜곡될 수 있으므로 이 값이 기준.
            try:
                eff_pct = self.bot.tracker.get_effective_pnl_pct()
                eff_dd = self.bot.tracker.get_effective_drawdown()
                lines.append(f"실효 손익률(실현+평가, 결제무관): {eff_pct:+.2f}% · drawdown {eff_dd:.2f}%")
            except Exception:
                pass
            lines.append(f"긴급홀딩: {'ON' if self.bot.risk.is_emergency_hold() else 'OFF'}")
            lines.append(f"AI 호출: claude {usage['claude']} | codex {usage['codex']} | agy {usage['agy']} | 합계 {usage['total']}")

            # KR
            lines.append("")
            lines.append("── KR 한국 ──")
            bal = self.bot.client.get_balance()
            kr_cash = bal["cash"]            # 즉시 매수 가능 (settled)
            kr_total_cash = bal.get("total_cash", kr_cash)  # 미정산(T+2) 포함
            kr_pending = max(kr_total_cash - kr_cash, 0)
            kr_eval = bal["total_eval"]
            kr_pending_note = (f" (즉시매수 {kr_cash:,}원 + 미정산 {kr_pending:,}원)"
                               if kr_pending > 0 else "")
            lines.append(
                f"예수금: {kr_total_cash:,}원{kr_pending_note} | "
                f"평가: {kr_eval:,}원 | 합계: {kr_total_cash + kr_eval:,}원"
            )
            if bal.get("holdings"):
                for h in bal["holdings"]:
                    lines.append(f"  {h['name']}({h['code']}): {h['qty']}주 | {h['avg_price']:,}→{h['current_price']:,} | {h['profit_rate']:+.1f}%")
            else:
                lines.append("  보유 종목 없음")

            # US
            lines.append("")
            lines.append("── US 미국 ──")
            fx = self.bot.client.get_usd_krw_rate()
            us_cash_settled = 0.0   # 즉시 매수 가능 (T+1 결제 완료)
            us_eval = 0.0
            us_eval_krw = 0
            try:
                us = self.bot.client.get_us_balance()
                us_cash_settled = us.get("cash_usd", 0.0)
                us_eval = sum(h.get("eval_amount", h.get("current_price", 0) * h.get("qty", 0))
                              for h in us.get("holdings", []))
                us_eval_krw = us.get("total_eval_krw", 0) or int(us_eval * fx)
                #: 한투 앱 외화 예수금과 일치하도록 settled cash만 표시.
                # sell_pending은 매수 미정산과 짝이라 자산에 합산하면 이중 카운트 (v6 패치).
                us_total_usd = us_cash_settled + us_eval

                lines.append(
                    f"예수금: ${us_cash_settled:.2f} ({int(us_cash_settled * fx):,}원)"
                )
                lines.append(
                    f"평가: ${us_eval:.2f} ({us_eval_krw:,}원 · 한투환산) | "
                    f"합계(자산): ${us_total_usd:.2f}"
                )
                # 매수·매도 미정산 짝지어 표시 — 자산에 미반영 (T+N 정산 시 상쇄)
                us_pending_buy_krw = us.get("unsettled_buy_krw", 0) or 0
                us_pending_sell_krw = us.get("unsettled_sell_krw", 0) or 0
                if us_pending_buy_krw > 0 or us_pending_sell_krw > 0:
                    net = us_pending_sell_krw - us_pending_buy_krw
                    lines.append(
                        f"  ※ 정산대기 — 매수 {us_pending_buy_krw:,}원 / 매도 {us_pending_sell_krw:,}원 "
                        f"(순효과 {net:+,}원, T+N 결제 후 자산 반영)"
                    )
                if us.get("holdings"):
                    for h in us["holdings"]:
                        lines.append(f"  {h['name']}({h['ticker']}): {h['qty']}주 | ${h['avg_price']:.2f}→${h['current_price']:.2f} | {h['profit_rate']:+.1f}%")
                else:
                    lines.append("  보유 종목 없음")
            except Exception:
                lines.append("  조회 실패")

            # 총자산 — bot_money_helpers.compute_total_equity 사용.
            # 한투 tot_asst_amt가 KR T+2 미정산을 누락하는 경우가 있어 직접 합산이 정답
            #: 이전 tot_asst_amt 단독 사용으로 약 4만원 누락되던 버그 수정).
            try:
                from zusik.analysis.bot_money_helpers import compute_total_equity
                eq = compute_total_equity(bal, us, fx)
                grand_total = eq["total"]
                breakdown = (f"KR {kr_total_cash + kr_eval:,} + "
                              f"US {eq['us_total_krw']:,} (${eq['us_total_usd']:.2f} × {fx:,.0f})")
                lines.append("")
                lines.append(f"총자산 ≈ {grand_total:,}원")
                lines.append(f"   ㄴ {breakdown}")
                if eq.get("vs_hantu_diff"):
                    lines.append(f"   (한투 tot_asst_amt 대비 +{eq['vs_hantu_diff']:,}원 — KR 미정산 보정)")
            except Exception:
                grand_total = (kr_total_cash + kr_eval
                                + int((us_cash_settled + us_eval) * fx))
                lines.append("")
                lines.append(f"총자산 ≈ {grand_total:,}원 (직접 합산)")

            # 수익 분해 (실현/미실현/수수료/환차익) — 표시 전용, 매매 평가에 미사용
            try:
                kr_unrealized = sum((h.get("current_price", 0) - h.get("avg_price", 0))
                                     * h.get("qty", 0) for h in self.bot.client.get_balance().get("holdings", []))
                us_unreal_usd = sum((h.get("current_price", 0) - h.get("avg_price", 0))
                                     * h.get("qty", 0) for h in self.bot.client.get_us_balance().get("holdings", []))
                unrealized_krw = int(kr_unrealized + us_unreal_usd * fx)
                summary = self.bot.tracker.get_effective_pnl_summary(grand_total, unrealized_krw)

                # 손실/수익 매도 분리: "잘못 매도" 손실이
                # 좋은 매도에 가려져 net +값으로 보이는 문제 → 분리 표시로 명확화
                sells = [t for t in self.bot.tracker._trades if t.get("type") == "sell"]
                bad_sells = [t for t in sells if (t.get("realized_pnl") or 0) < 0]
                good_sells = [t for t in sells if (t.get("realized_pnl") or 0) > 0]
                bad_pnl = sum((t.get("realized_pnl") or 0) for t in bad_sells)
                good_pnl = sum((t.get("realized_pnl") or 0) for t in good_sells)

                # 현재 예수금 (KR settled + USD settled × FX). 보유 평가는 제외.
                # 명목 증가가 마이너스로 빠질 때 그 손실이 예수금에 얼마나 반영됐는지
                # 가시화하기 위해 함께 표시.
                deposit_now = kr_total_cash + int(us_cash_settled * fx)
                deposit_vs_input = deposit_now - summary['total_deposits']

                lines.append("")
                lines.append("── 수익 분해 (표시 전용) ──")
                lines.append(f"  누적 입금: {summary['total_deposits']:,}원")
                lines.append(f"  현재 예수금: {deposit_now:,}원 "
                             f"(입금 대비 {deposit_vs_input:+,}원)")
                lines.append(f"  명목 증가: {summary['apparent_gain']:+,}원 "
                             f"({summary['apparent_gain']/max(summary['total_deposits'],1)*100:+.2f}%)")
                if abs(summary.get('fx_and_other_effect', 0)) > 200000:
                    lines.append("  명목/총자산은 미국 T+2 미결제로 일시 축소 표시될 수 있음 "
                                 "— 아래 '실효 순수익'이 진짜 손익")
                lines.append(f"  ├ 실현 수익(net, 수수료 차감 후): {summary['realized_total']:+,}원")
                lines.append(f"  │   ├ 손실 매도 {len(bad_sells)}건: {bad_pnl:+,}원")
                lines.append(f"  │   └ 수익 매도 {len(good_sells)}건: {good_pnl:+,}원")
                lines.append(f"  ├ 미실현 평가차익: {summary['unrealized_krw']:+,}원")
                lines.append(f"  └ 환차익·기타: {summary['fx_and_other_effect']:+,}원")
                lines.append("      (USD 환전 spread + FX 차이 + 미국 T+2 미결제 정산 타이밍 — 결제 완료 시 상쇄)")
                lines.append(f"  누적 매매 수수료(record): {summary['total_fees']:,}원")
                lines.append(f"  실효 순수익(실현+미실현): {summary['effective_total']:+,}원")
            except Exception:
                pass

            # 감시 종목
            lines.append("")
            lines.append("── 감시 종목 ──")
            kr_names = ", ".join(s.get("name", s.get("code", "")) for s in self.bot.kr_stocks)
            us_names = ", ".join(s.get("name", s.get("ticker", "")) for s in self.bot.us_stocks)
            lines.append(f"KR: {kr_names or '없음'}")
            lines.append(f"US: {us_names or '없음'}")

            return "\n".join(lines)
        except Exception as e:
            return f"상태 조회 실패: {e}"

    # ── 모드 변경 ──

    def _handle_mode(self, mode: str) -> str:
        from zusik.core.trading_mode import MODE_PROFILES, get_mode_summary
        mode = mode.lower()
        if mode not in MODE_PROFILES:
            return f"알 수 없는 모드: {mode}\n가능: {', '.join(MODE_PROFILES.keys())}"
        self.bot._apply_mode_change(mode)
        return f"모드 변경: {mode.upper()}\n{get_mode_summary(mode)}"

    # ── 수동 매수/매도 ──

    def _handle_buy(self, args: list) -> str:
        market = args[0].upper()
        code = args[1].upper()
        amount = int(args[2]) if len(args) > 2 else 0

        if market == "KR" and amount > 0:
            price_info = self.bot.client.get_current_price(code)
            price = price_info["price"]
            name = price_info.get("name", code)
            qty = amount // price
            if qty <= 0:
                return f"금액 부족: {amount:,}원으로 {price:,}원 종목 매수 불가"
            result = self.bot._submit_kr_market_buy(code, qty, price)
            if result.get("success"):
                qty = self.bot._submitted_order_qty(result, qty)
                if qty <= 0:
                    return "매수 실패: 성공 응답에 실제 전송 수량 없음"
                # 수동 주문도 봇 회계에 기록 — 없으면 포지션 보호(본전/트레일링)와
                # 실현손익·패턴 통계가 이 보유를 모르거나 추정치로 어긋남
                self._record_manual_buy(code, name, qty, price)
                self._send_alert("buy", name, code, qty, f"{price:,}원")
            return f"매수 주문: {name} {qty}주 ({amount:,}원)\n결과: {result.get('message', 'OK')}"
        elif market == "US":
            price_info = self.bot.client.get_us_current_price(code, "NASD")
            price = price_info["price"]
            name = price_info.get("name", code)
            if amount > 0:
                qty = int(amount / price) if price > 0 else 0
            else:
                qty = 1
            if qty <= 0:
                return f"매수 불가: ${price}"
            buy_price = round(price * 1.005, 2)
            result = self.bot.client.buy_us_limit(code, qty, buy_price, "NASD")
            if result.get("success"):
                fx = self.bot.client.get_usd_krw_rate()
                self._record_manual_buy(code, name, qty, buy_price, price_krw=int(buy_price * fx))
                self._send_alert("buy", name, code, qty, f"${buy_price}")
            return f"매수 주문: {name} {qty}주 @ ${buy_price}\n결과: {result.get('message', 'OK')}"
        return "사용법: /매수 KR 005930 50000  또는  /매수 US SOFI"

    def _record_manual_buy(self, code: str, name: str, qty: int, price, price_krw: int = 0):
        """수동 매수를 tracker/positions 에 기록 — 실패해도 주문 결과 보고는 막지 않음."""
        try:
            self.bot.positions.record_buy(code, name, qty, price)
        except Exception:
            logger.debug("수동 매수 포지션 기록 실패: %s", code, exc_info=True)
        try:
            self.bot.tracker.record_buy(code, name, qty, int(price_krw or price), False, "수동 명령")
        except Exception:
            logger.debug("수동 매수 tracker 기록 실패: %s", code, exc_info=True)

    def _record_manual_sell(self, code: str, name: str, qty: int, sell_price, avg_price,
                            sell_krw: int = 0, avg_krw: int = 0):
        """수동 매도를 tracker/positions 에 기록 — 스냅샷 추정 대신 실제 체결가 기준 P&L."""
        try:
            self.bot.tracker.record_sell(code, name, qty, int(sell_krw or sell_price),
                                         int(avg_krw or avg_price), "수동 명령")
        except Exception:
            logger.debug("수동 매도 tracker 기록 실패: %s", code, exc_info=True)
        try:
            self.bot.positions.record_sell(code, qty)
        except Exception:
            logger.debug("수동 매도 포지션 기록 실패: %s", code, exc_info=True)

    def _send_alert(self, side: str, name: str, code: str, qty: int, price_str: str):
        """Discord 채널에 매매 알림."""
        if not self.bot.discord:
            return
        
        try:
            # price_str에서 숫자만 추출
            import re
            price_val = int(re.sub(r'[^0-9]', '', price_str))
        except Exception:
            price_val = 0

        self.bot.discord.notify_trade(
            side=side,
            stock_name=name,
            stock_code=code,
            qty=qty,
            price=price_val,
            reason="수동 주문"
        )

    def _handle_sell(self, args: list) -> str:
        market = args[0].upper()
        code = args[1].upper()

        if market == "KR":
            bal = self.bot.client.get_balance()
            holding = next((h for h in bal["holdings"] if h["code"] == code), None)
            if not holding:
                return f"{code} 보유 없음"
            name = holding.get("name", code)
            result = self.bot.client.sell_market(code, holding["qty"])
            if result.get("success"):
                self._record_manual_sell(code, name, holding["qty"],
                                         holding["current_price"], holding.get("avg_price", 0))
                self._send_alert("sell", name, code, holding["qty"], f"{holding['current_price']:,}원")
            return f"매도: {name} {holding['qty']}주\n결과: {result.get('message', 'OK')}"
        elif market == "US":
            us = self.bot.client.get_us_balance()
            holding = next((h for h in us["holdings"] if h["ticker"] == code), None)
            if not holding:
                return f"{code} 보유 없음"
            name = holding.get("name", code)
            result = self.bot.client.sell_us_limit(code, holding["qty"], holding["current_price"] * 0.995, "NASD")
            if result.get("success"):
                fx = self.bot.client.get_usd_krw_rate()
                cur, avg = holding["current_price"], holding.get("avg_price", 0)
                self._record_manual_sell(code, name, holding["qty"], cur, avg,
                                         sell_krw=int(cur * fx), avg_krw=int(avg * fx))
                self._send_alert("sell", name, code, holding["qty"], f"${holding['current_price']:.2f}")
            return f"매도: {name} {holding['qty']}주\n결과: {result.get('message', 'OK')}"
        return "사용법: /매도 KR 005930  또는  /매도 US SOFI"

    # ── 리포트 ──

    def _handle_report(self) -> str:
        import threading
        threading.Thread(
            target=self.bot._send_claude_daily_report, daemon=True
        ).start()
        return "AI 분석 리포트 생성 중... 잠시 후 채널에 전송됩니다."

    # ── 긴급 홀딩 ──

    def _handle_emergency_hold(self) -> str:
        self.bot.risk.activate_emergency_hold("수동 긴급 홀딩")
        return "긴급 홀딩 활성화 — 모든 매매 중단"

    def _handle_emergency_release(self) -> str:
        self.bot.risk.deactivate_emergency_hold()
        return "긴급 홀딩 해제 — 매매 재개"

    def _handle_loss_release(self) -> str:
        """일일 손실한도 매매 중단 해제 — 해제한 시장은 오늘 재발동하지 않는다.

        재발동 방지가 없으면 다음 사이클에 같은 손실로 즉시 재중단돼 명령이 무의미.
        하드스톱/긴급홀딩/드로우다운 가드는 이 해제와 무관하게 유지된다."""
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        halted = dict(getattr(self.bot, "_daily_loss_halted", {}) or {})
        active = [m for m, d in halted.items() if d == today]
        released = active if active else ["ALL", "KR", "US"]  # 중단 전 선제 해제도 허용
        for m in released:
            self.bot._daily_loss_released[m] = today
            self.bot._daily_loss_halted.pop(m, None)
        scope = ", ".join(released)
        state = "중단 상태였던" if active else "선제 해제 —"
        return (f"일일 손실한도 해제 ({state} {scope})\n"
                f"오늘 남은 시간 동안 해당 시장의 손실한도 중단은 재발동하지 않습니다.\n"
                f"종목 하드스톱·긴급홀딩·드로우다운 가드는 그대로 유지됩니다.")

    # ── 업데이트 반영 ──

    def _handle_update(self) -> str:
        """origin 의 새 push 를 적용 — git pull + 봇 재시작. (메신저 '업데이트' 명령)."""
        import subprocess
        try:
            pull = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
            if pull.returncode != 0:
                return f"git pull 실패:\n{(pull.stderr or pull.stdout)[:400]}"
            # 재시작이 성공하면 이 프로세스가 죽어 아래 성공 메시지는 전달 못 할 수 있음(정상).
            # 성공 확인은 워치독/기동 알림 담당 — 여기선 실패(sudo 거부 등)만 정확히 보고.
            rst = subprocess.run(["sudo", "systemctl", "restart", "zusik"],
                                 capture_output=True, text=True, timeout=10)
            if rst.returncode != 0:
                return (f"git pull 성공, 재시작 실패(rc={rst.returncode}) — 구버전이 계속 실행 중!\n"
                        f"{(rst.stderr or rst.stdout)[:300]}")
            return f"업데이트 적용 + 재시작\n{pull.stdout[:400]}"
        except Exception as e:
            return f"업데이트 실패: {e}"

    # ── 진단 (헬스 / 점검) ──

    def _handle_health(self) -> str:
        """코어 alive·마지막 tick·LLM provider·워치독 최근 체크 요약 (파일 기반, API 0)."""
        import json
        from datetime import datetime
        from zusik import paths

        def _read(name):
            try:
                with open(paths.data_path(name), encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                return {}

        lines = ["=== 시스템 헬스 ==="]
        # 코어 heartbeat (status.json generated_at)
        snap = _read("status.json")
        gen = snap.get("generated_at", "")
        try:
            age = (datetime.now() - datetime.strptime(gen, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            core = f"마지막 tick {age:.0f}분 전" + (" — 멈춤 의심" if age > 10 else " 정상")
        except Exception:
            core = "상태 파일 없음 (봇 미가동?)"
        lines.append(f"코어: {core}")
        # LLM 가용성
        llm = _read("llm_health.json")
        st = llm.get("status", "ok")
        if st == "down":
            lines.append(f"LLM: 작동 불가 ({llm.get('last_reason','')[:50]}) — login/쿼터 확인")
        else:
            cf = llm.get("consecutive_fail", 0)
            lines.append(f"LLM: 정상" + (f" (최근 연속실패 {cf})" if cf else ""))
        # provider 상태 (config 활성 + cooldown 파일, 실호출 없음 — 라이브 검증은 /점검)
        try:
            import os as _os, tempfile as _tf, time as _tm
            ai_cfg = (self.bot.config.get("ai_providers", {}) or {})

            def _cd_min(fname):
                try:
                    with open(_os.path.join(_tf.gettempdir(), fname)) as f:
                        rem = (float(f.read().strip() or 0) - _tm.time()) / 60
                        return rem if rem > 0 else 0
                except Exception:
                    return 0
            prov = ["claude"]
            if not ai_cfg.get("disable_agy", False):
                prov.append("agy")
            cx = _cd_min("codex_cooldown_until.txt")
            prov.append(f"codex(제한 {cx:.0f}분)" if cx > 0 else "codex")
            lines.append("provider: " + " · ".join(prov) + "  (실호출 검증 /점검)")
        except Exception:
            pass
        # AI 호출량 (오늘)
        try:
            usage = self.bot.cost.get_today_usage()
            lines.append(f"AI 호출(오늘): claude {usage['claude']} · agy {usage.get('agy','-')} · codex {usage['codex']}")
        except Exception:
            pass
        # 워치독 마지막 체크
        wd = _read("watchdog_state.json")
        if wd.get("last_check"):
            flags = []
            if wd.get("core_down"):
                flags.append("코어다운")
            if wd.get("llm_down"):
                flags.append("LLM다운")
            lines.append(f"워치독: 마지막 체크 {wd['last_check'][:16].replace('T',' ')}"
                         + (f" — {', '.join(flags)}" if flags else " — 이상 없음"))
        else:
            lines.append("워치독: 기록 없음 (timer 미설치? bash deploy/setup_service.sh)")
        lines.append("\n실시간 외부점검은 /점검 (KIS·LLM·메신저 실제 호출)")
        return "\n".join(lines)

    def _handle_healthcheck(self) -> str:
        """KIS 토큰·provider별 실제 응답·메신저·경로를 점검 (provider 개별 호출, 십수 초)."""
        try:
            from zusik.utils.healthcheck import healthcheck_text
            _code, text = healthcheck_text(self.bot.client, self.bot.config)
            return text
        except Exception as e:
            return f"점검 실패: {e}"

    # ── 도움말 (그룹별 메뉴) ──

    @staticmethod
    def _handle_help() -> str:
        return """**봇 명령어**

**조회**
/상태 — 자산·모드·시장온도·보유
/성과 — 누적 실효 수익·승률·매도 패턴/타이밍·선택 alpha
/종목 — 자동선별된 감시 종목 (수동 추가/제거는 자동선별이 덮어써 미지원)

**매매**
/매수 KR 005930 50000 — 수동 매수 (시장 코드 금액)
/매도 KR 005930 — 수동 매도

**분석·리포트**
/분석 005930 — 국내 · SOFI 미국 · BTC 암호화폐 · TYO:5253 해외(검색용)
/장전분석 KR — 장전 Claude 분석 즉시 발송
/리포트 — 일일 리포트 전송

**운영·제어**
/모드 aggressive — 트레이딩 모드 변경
/긴급홀딩 — 매매 즉시 중단 · /홀딩해제 — 재개
/손실해제 — 일일 손실한도 매매 중단 해제 (오늘 재발동 안 함)
/업데이트 — 최신 버전으로 업데이트(git pull + 재시작)

**진단**
/헬스 — 코어·LLM·워치독 상태 요약 (즉시, 파일 기반)
/점검 — KIS·LLM·메신저 실제 호출 점검 (몇 초)
/도움 — 이 도움말"""
