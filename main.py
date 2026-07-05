#!/usr/bin/env python3
from __future__ import annotations
"""한국 주식 자동매매 봇 진입점.

스케줄:
  08:40  장 시작 전 Discord 알림 (시장 동향 + 감시 종목)
  09:00~15:20  5분 간격 자동매매
  15:35  장 마감 후 Discord 일일 수익률 리포트
"""

import argparse
import os
import sys

from dotenv import load_dotenv

# .env를 가장 먼저 로드 — discord_bot 등 모듈이 import 시점에 환경변수를 읽기 때문
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env_path, override=True)

from zusik.core.bot import TradingBot, load_config
from zusik.clients.discord_notifier import DiscordNotifier
from zusik.utils.logger import setup_logger


def main():
    parser = argparse.ArgumentParser(description="한국 주식 자동매매 봇")
    parser.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    parser.add_argument("--once", action="store_true", help="1회만 실행")
    parser.add_argument("--status", action="store_true", help="보유 자산 확인")
    parser.add_argument("--backtest", action="store_true", help="전략 백테스트 리포트")
    parser.add_argument("--report", action="store_true", help="Claude AI 분석 리포트 (매매 없음)")
    parser.add_argument("--daily-report", action="store_true", help="일일 리포트를 Discord로 즉시 전송")
    parser.add_argument("--healthcheck", action="store_true",
                        help="KIS/AI/메신저/경로 점검 (셋업 직후·장 시작 전 권장)")
    parser.add_argument("--notify", action="store_true",
                        help="--healthcheck 결과 요약을 메신저로도 전송 (장전 cron 용)")
    args = parser.parse_args()

    # 브로커 선택 — .env 의 BROKER 로 활성 증권사 1개 선택(기본 kis). 여러 브로커 키(KIS_*,
    # TOSS_*, KIWOOM_*, SHINHAN_*)를 .env 에 함께 둬도 되고, 없으면 KIS_* 로 폴백한다.
    from zusik.clients.broker import (BROKER_ENV, account_no_required,
                                      resolve_broker_credentials)
    broker_name = os.getenv("BROKER", "kis").strip().lower()
    creds = resolve_broker_credentials(broker_name)

    # 토스는 계좌(accountSeq)를 API 로 자동 탐색 → client_id/secret 만 있으면 됨.
    need_acct = account_no_required(broker_name)
    if not creds["app_key"] or not creds["app_secret"] or (need_acct and not creds["account_no"]):
        env = BROKER_ENV.get(broker_name, BROKER_ENV["kis"])
        req = f"{env['key']}, {env['secret']}" + (f", {env['account']}" if need_acct else "")
        print(f"오류: .env 에 활성 브로커(BROKER={broker_name})의 API 키를 설정하세요.")
        print()
        print("  cp .env.example .env")
        print()
        print(f"  필수: {req}" + ("" if need_acct else "  (계좌는 자동 탐색)")
              + "  — 브로커별 키가 없으면 KIS_* 로 폴백")
        print("  브로커 현황: README '지원 브로커'")
        return

    config = load_config(args.config)
    setup_logger(config.get("log_level", "INFO"))

    # 무결성 트립와이어 — 디스크 코드가 커밋된 기준선(security_manifest.json)과 다르면
    # "악의적 함수 변형/악성코드 삽입" 의심으로 경보. SECURITY_STRICT=true면 시작 중단.
    try:
        import logging as _logging
        from security_lock import verify_files
        _ok, _mm = verify_files()
        if not _ok:
            _log = _logging.getLogger("security")
            _log.critical("무결성 위반 %d건 — 변조 의심: %s", len(_mm),
                          ", ".join(f"{r}({w})" for r, w in _mm[:8]))
            if os.getenv("SECURITY_STRICT", "false").lower() == "true":
                _log.critical("SECURITY_STRICT=true → 시작 중단 (기준선 갱신: python3 security_lock.py generate)")
                return
    except Exception:
        pass

    # 활성 브로커 인스턴스 생성. 실험 브로커(toss 등)는 동의·dry-run 가드가 클라이언트 내부에 있음.
    from zusik.clients.broker import create_broker
    try:
        client = create_broker(broker_name, **creds)
    except (NotImplementedError, ValueError) as e:
        print(str(e))
        from zusik.clients.broker import list_brokers_text
        print("\n" + list_brokers_text())
        sys.exit(1)

    # 메신저 알림: Discord/Telegram/Slack 중 설정된 백엔드로 동시 발송 (MultiNotifier).
    from zusik.clients.notifier import MultiNotifier, TelegramNotifier, SlackNotifier
    backends = []
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if discord_url:
        backends.append(DiscordNotifier(discord_url))
    telegram = None
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat:
        telegram = TelegramNotifier(tg_token, tg_chat)
        backends.append(telegram)
    slack = None
    slack_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if slack_url:
        slack = SlackNotifier(slack_url, app_token=os.getenv("SLACK_APP_TOKEN", ""))
        backends.append(slack)

    if len(backends) > 1:
        discord = MultiNotifier(backends)
    elif backends:
        discord = backends[0]
    else:
        discord = None  # 미설정 시 Discord Bot 기반 _BotNotifierFallback 자동 적용

    # Telegram 양방향: getUpdates 폴링 → commands.json 큐 (응답은 notifier로 Telegram에도 도달)
    if telegram:
        try:
            from zusik.clients.discord_commander import queue_command
            telegram.start_command_polling(queue_command)
        except Exception:
            pass

    # Slack 양방향: Socket Mode(WSS) 명령 수신 → commands.json 큐 (SLACK_APP_TOKEN 설정 시)
    if slack:
        try:
            from zusik.clients.discord_commander import queue_command
            slack.start_command_polling(queue_command)
        except Exception:
            pass

    # 헬스체크는 무거운 TradingBot 없이 빠르게 — KIS/AI/메신저/경로만 점검하고 종료
    if args.healthcheck:
        from zusik.utils.healthcheck import run_healthcheck
        sys.exit(run_healthcheck(client, config, discord=discord, notify=args.notify))

    bot = TradingBot(client, config, discord=discord)

    if args.status:
        bot.status()
    elif args.backtest:
        bot.backtest_report()
    elif args.report:
        bot.claude_report()
    elif args.daily_report:
        bot.post_market_report()
    elif args.once:
        bot.run_once()
    else:
        bot.start()


if __name__ == "__main__":
    main()
