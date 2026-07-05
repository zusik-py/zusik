from __future__ import annotations
"""Discord 봇 — 슬래시 명령으로 주식 봇 제어.

슬래시 자동완성 + 매매 판단 이유 설명(verbose).
"""

import asyncio
import logging
import os
import threading

import discord
from discord import app_commands

from zusik.clients.discord_commander import DiscordCommander

logger = logging.getLogger(__name__)

# 봇 소유자 Discord ID — .env에 DISCORD_OWNER_ID 설정 필수
def _parse_owner_id(raw) -> int:
    """빈 값('')·비숫자면 0 — .env.example 그대로 복사해도 import 크래시 없이 fail-closed."""
    try:
        return int(raw or 0)
    except (ValueError, TypeError):
        return 0


_OWNER_ID = _parse_owner_id(os.getenv("DISCORD_OWNER_ID"))

_discord_bot_ref = None  # 전역 봇 참조 (알림용)


def _is_owner(interaction: discord.Interaction) -> bool:
    """명령 실행자가 봇 소유자인지 확인.

    OWNER_ID 미설정이면 거부(fail-closed). 이전의 '서버 관리자 폴백'은 관리자
    계정 탈취 = 원격 매매·업데이트 권한이라 제거 — 매매 명령을 쓰려면
    DISCORD_OWNER_ID 를 명시해야 한다. 알림(webhook)은 권한과 무관하게 동작.
    """
    if _OWNER_ID == 0:
        return False
    return interaction.user.id == _OWNER_ID


# ── 알림 채널 선택 ──
# 봇이 알림을 보낼 채널을 사용자가 정할 수 있게 한다. 우선순위:
#   1) 런타임 지정 (/알림채널 명령 → data/discord_channel.txt 영속화)
#   2) env DISCORD_CHANNEL_ID
#   3) 자동 (보낼 수 있는 첫 텍스트 채널) — 기존 동작
def _channel_store_path() -> str:
    from zusik.paths import data_path
    return data_path("discord_channel.txt")


def _load_pinned_channel_id() -> int:
    """지정된 알림 채널 id (런타임 파일 > env). 없으면 0."""
    try:
        with open(_channel_store_path(), encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return int(v)
    except Exception:
        pass
    try:
        return int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")
    except ValueError:
        return 0


def _save_pinned_channel_id(channel_id: int) -> None:
    """런타임 지정 채널 id 영속화 (재시작에도 유지)."""
    try:
        os.makedirs(os.path.dirname(_channel_store_path()), exist_ok=True)
        with open(_channel_store_path(), "w", encoding="utf-8") as f:
            f.write(str(int(channel_id)))
    except Exception as e:
        logger.warning("알림 채널 저장 실패: %s", e)


class ZusikBot(discord.Client):
    def __init__(self):
        # 슬래시 명령(interactions)만 사용 — message_content 같은 특권 인텐트 불필요.
        # 특권 인텐트를 켜면 개발자 포털 토글이 꺼진 환경에서 bot.run()이
        # PrivilegedIntentsRequired로 죽어 on_ready가 영영 안 떠 알림 채널이 안 붙는다.
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.commander = None
        self._alert_channel = None

    async def setup_hook(self):
        # 글로벌 sync 대신 on_ready에서 guild별 sync — 글로벌은 등록까지 최대 1시간 소요
        pass

    async def on_ready(self):
        logger.info("Discord 봇 연결: %s", self.user.name)
        # 1) 사용자가 지정한 채널(런타임 /알림채널 또는 env DISCORD_CHANNEL_ID) 우선
        pinned = _load_pinned_channel_id()
        if pinned:
            ch = self.get_channel(pinned)
            guild_me = getattr(getattr(ch, "guild", None), "me", None)
            if ch is not None and guild_me is not None \
                    and ch.permissions_for(guild_me).send_messages:
                self._alert_channel = ch
                logger.info("알림 채널(지정): #%s", ch.name)
            else:
                logger.warning("지정 알림 채널(id=%s)을 못 찾거나 권한 없음 — 자동 선택으로 폴백", pinned)
                pinned = None
        # 2) 폴백: 보낼 수 있는 첫 텍스트 채널 (기존 동작)
        if not self._alert_channel:
            for guild in self.guilds:
                for ch in guild.text_channels:
                    if ch.permissions_for(guild.me).send_messages:
                        self._alert_channel = ch
                        logger.info("알림 채널(자동): #%s", ch.name)
                        break
                if self._alert_channel:
                    break
        # 3) 슬래시 명령 guild별 즉시 동기화 (글로벌 sync는 최대 1시간 소요)
        for guild in self.guilds:
            try:
                await self.tree.sync(guild=guild)
                logger.info("Discord 슬래시 명령 동기화 완료: %s", guild.name)
            except Exception as e:
                logger.warning("Discord 슬래시 명령 동기화 실패 (%s): %s", guild.name, e)

    async def _send_alert(self, message: str = "", embeds: list | None = None):
        """알림 채널로 메시지 전송."""
        if self._alert_channel:
            try:
                discord_embeds = []
                if embeds:
                    for e in embeds:
                        # dict -> discord.Embed
                        de = discord.Embed.from_dict(e)
                        discord_embeds.append(de)
                
                await self._alert_channel.send(content=message, embeds=discord_embeds)
            except Exception as e:
                logger.warning("Discord 알림 전송 실패: %s", e)


def send_trade_alert(side: str, name: str, code: str, qty: int, price, reason: str = ""):
    """매매 체결 시 Discord으로 알림 (어디서든 호출 가능)."""
    bot = _discord_bot_ref
    if not bot or not bot._alert_channel:
        return

    if side in ("buy", "long_term_buy"):
        emoji = "매수" if side == "buy" else "장기매수"
    else:
        emoji = "매도"

    msg = f"**{emoji}** `{name}({code})` {qty}주 @ {price}\n"
    if reason:
        msg += f"```{reason}```"

    # Discord 메시지 한도 2000 chars — 길면 분할 송신.
    if len(msg) <= 1900:
        asyncio.run_coroutine_threadsafe(bot._send_alert(msg), bot.loop)
    else:
        chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
        for chunk in chunks:
            asyncio.run_coroutine_threadsafe(bot._send_alert(chunk), bot.loop)


def send_bot_message(message: str):
    """범용 Discord Bot 메시지 전송 (webhook 불필요)."""
    bot = _discord_bot_ref
    if not bot or not bot._alert_channel:
        return
    # 2000자 제한
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        asyncio.run_coroutine_threadsafe(bot._send_alert(chunk), bot.loop)


class _UpdateView(discord.ui.View):
    """커밋 업데이트 버튼 (적용/스킵/변경사항)."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="업데이트 적용", style=discord.ButtonStyle.green, emoji=None)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("권한 없음 (소유자만 가능)", ephemeral=True)
            return
        await interaction.response.send_message("**업데이트 적용 중...**")
        import subprocess
        from zusik.paths import ROOT
        try:
            pull = subprocess.run(["git", "pull"], capture_output=True, text=True,
                                  timeout=30, cwd=str(ROOT))
            subprocess.run(["sudo", "systemctl", "restart", "zusik"],
                          capture_output=True, text=True, timeout=10)
            await interaction.channel.send(
                f"**업데이트 완료**\n```\n{pull.stdout[:500]}\n```\n봇 재시작됨"
            )
        except Exception as e:
            await interaction.channel.send(f"**업데이트 실패**: {e}")
        self.stop()

    @discord.ui.button(label="나중에", style=discord.ButtonStyle.grey, emoji=None)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("**건너뜀** — `/업데이트`로 나중에 적용 가능")
        self.stop()

    @discord.ui.button(label="변경사항", style=discord.ButtonStyle.blurple, emoji=None)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        import subprocess
        from zusik.paths import ROOT
        try:
            diff = subprocess.run(["git", "log", "-1", "--stat"],
                                  capture_output=True, text=True, timeout=10,
                                  cwd=str(ROOT))
            detail = diff.stdout[:1800] if diff.stdout else "변경사항 없음"
        except Exception:
            detail = "조회 실패"
        await interaction.response.send_message(f"```\n{detail}\n```")


def send_update_alert(commit_hash: str, msg: str, author: str, when: str,
                       files: str, commit_iso: str = "") -> bool:
    """커밋 감지 → Discord embed 알림.

    "실시간" 동작:
      - 알림 줄()은 송신 시각 기준 <t:UNIX:R> → 항상 "방금"으로 시작해
        클라이언트가 자동으로 "5분 전 / 1시간 전" 갱신
      - 커밋 줄()은 commit 시각 기준 <t:UNIX:R> → 봇이 늦게 감지해도
        commit 자체의 나이가 그대로 표시
      - 두 시각이 다를 수 있어 분리 표시 (송신 vs 작성)
      - embed.timestamp는 송신 시각 → footer도 "Today at 21:30" 등 알림 도착 시점

    Args:
        commit_iso: ISO8601 커밋 시각. 없으면 commit 줄은 fallback %cr.
    """
    bot = _discord_bot_ref
    if not bot or not bot._alert_channel:
        logger.warning("업데이트 알림 스킵: bot=%s, channel=%s",
                        bool(bot), bool(bot._alert_channel) if bot else False)
        return False

    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    now_unix = int(now.timestamp())

    # commit 시각 마크업
    commit_markup = ""
    if commit_iso:
        try:
            dt = _dt.fromisoformat(commit_iso)
            cu = int(dt.timestamp())
            commit_markup = f"<t:{cu}:R> (<t:{cu}:f>)"
        except Exception:
            pass

    description = (
        f"`{commit_hash}` {msg}\n"
        f"{author}\n"
        f"{files}\n"
        f"알림: <t:{now_unix}:R>\n"
    )
    if commit_markup:
        description += f"커밋: {commit_markup}\n"
    else:
        description += f"커밋: {when}\n"
    description += "────────────────\n업데이트를 적용하시겠습니까?"

    embed = discord.Embed(
        title="새 업데이트",
        description=description,
        color=0x5865F2,
    )
    embed.timestamp = now  # footer = 알림 송신 시각

    async def _send():
        try:
            view = _UpdateView()
            ch = bot._alert_channel
            if ch is None:
                logger.warning("업데이트 알림 실패: 채널 없음")
                return
            await ch.send(embed=embed, view=view)
            logger.info("업데이트 알림 Discord 전송 완료: %s", commit_hash)
        except Exception as e:
            logger.warning("업데이트 알림 실패: %s", e, exc_info=True)

    try:
        asyncio.run_coroutine_threadsafe(_send(), bot.loop)
        return True
    except Exception as e:
        logger.warning("업데이트 알림 스케줄 실패: %s", e)
        return False


def start_discord_bot(trading_bot, token: str = ""):
    token = token or os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.info("Discord 봇 토큰 없음 — 비활성화")
        return

    global _discord_bot_ref
    bot = ZusikBot()
    commander = DiscordCommander(trading_bot)
    bot.commander = commander
    _discord_bot_ref = bot

    # ── 슬래시 명령 등록 ──

    # 소유자 전용 명령 (매매/모드/긴급 등)
    _OWNER_ONLY_CMDS = {"매수", "매도", "모드", "긴급홀딩", "홀딩해제", "손실해제", "종목"}

    async def _run_and_reply(interaction: discord.Interaction, cmd: str):
        """모든 명령을 defer → 백그라운드 → followup 패턴으로."""
        # 민감한 명령은 소유자만 허용
        cmd_prefix = cmd.split()[0] if cmd else ""
        if cmd_prefix in _OWNER_ONLY_CMDS and not _is_owner(interaction):
            await interaction.response.send_message(
                "권한 없음 — 이 명령은 봇 소유자만 실행 가능합니다.", ephemeral=True
            )
            return
        await interaction.response.defer()
        import threading
        def _run():
            result = commander._execute(cmd)
            # 1900-char 코드블록 단위로 분할 — 길이 제한 없이 전체 표시.
            # 이전 1900자 잘라 "..." 붙이던 로직 제거.
            chunks = [result[i:i+1900] for i in range(0, max(len(result), 1), 1900)]
            async def _send():
                first = True
                for chunk in chunks:
                    if first:
                        await interaction.followup.send(f"```\n{chunk}\n```")
                        first = False
                    else:
                        await interaction.channel.send(f"```\n{chunk}\n```")
            asyncio.run_coroutine_threadsafe(_send(), bot.loop)
        threading.Thread(target=_run, daemon=True).start()

    @bot.tree.command(name="상태", description="자산/모드/시장온도 확인")
    async def cmd_status(interaction: discord.Interaction):
        await _run_and_reply(interaction, "상태")

    @bot.tree.command(name="종목", description="현재 자동선별된 감시 종목 목록")
    async def cmd_stock(interaction: discord.Interaction):
        await _run_and_reply(interaction, "종목 목록")

    @bot.tree.command(name="모드", description="트레이딩 모드 변경")
    @app_commands.describe(mode="모드 선택")
    @app_commands.choices(mode=[
        app_commands.Choice(name="seed (씨앗)", value="seed"),
        app_commands.Choice(name="yolo (소액올인)", value="yolo"),
        app_commands.Choice(name="aggressive (공격)", value="aggressive"),
        app_commands.Choice(name="active (적극)", value="active"),
        app_commands.Choice(name="balanced (균형)", value="balanced"),
        app_commands.Choice(name="growth (성장)", value="growth"),
        app_commands.Choice(name="conservative (보수)", value="conservative"),
    ])
    async def cmd_mode(interaction: discord.Interaction, mode: str):
        await _run_and_reply(interaction, f"모드 {mode}")

    @bot.tree.command(name="매수", description="수동 매수")
    @app_commands.describe(market="KR/US", code="종목코드", amount="금액(원)")
    async def cmd_buy(interaction: discord.Interaction, market: str, code: str, amount: int):
        await _run_and_reply(interaction, f"매수 {market} {code} {amount}")

    @bot.tree.command(name="매도", description="수동 매도")
    @app_commands.describe(market="KR/US", code="종목코드")
    async def cmd_sell(interaction: discord.Interaction, market: str, code: str):
        await _run_and_reply(interaction, f"매도 {market} {code}")

    @bot.tree.command(name="리포트", description="일일 리포트 전송")
    async def cmd_report(interaction: discord.Interaction):
        await _run_and_reply(interaction, "리포트")

    @bot.tree.command(name="장전분석", description="KR/US 장 시작 전 Claude 분석 즉시 발송 (가드 무시)")
    @app_commands.describe(market="시장 선택")
    @app_commands.choices(market=[
        app_commands.Choice(name="KR (한국)", value="KR"),
        app_commands.Choice(name="US (미국)", value="US"),
    ])
    async def cmd_pre_report(interaction: discord.Interaction, market: str):
        # 읽기 명령처럼 보이지만 당일 sentiment 파일을 갱신해 매수 게이트를 바꾸는
        # 상태 변경 명령(force=True) — 소유자 전용. LLM/웹검색 강제 호출 비용도 있음.
        if not _is_owner(interaction):
            await interaction.response.send_message("권한 없음 (소유자만 가능)", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{market} 장전 분석 강제 실행** (Claude 호출 ~1분 소요)"
        )
        import threading
        def _run():
            try:
                trading_bot._send_pre_market_report(market, force=True)
            except Exception as exc:
                # except 블록 종료 시 파이썬이 e 를 del 하므로, 나중에 실행되는 _err 코루틴이
                # e 를 참조하면 NameError. 메시지를 일반 지역변수로 캡처해 클로저에 안전 전달.
                err_msg = str(exc)
                async def _err():
                    await interaction.channel.send(f"실행 실패: {err_msg}")
                asyncio.run_coroutine_threadsafe(_err(), bot.loop)
        threading.Thread(target=_run, daemon=True).start()

    @bot.tree.command(name="긴급홀딩", description="모든 매매 즉시 중단")
    async def cmd_hold(interaction: discord.Interaction):
        await _run_and_reply(interaction, "긴급홀딩")

    @bot.tree.command(name="홀딩해제", description="매매 재개")
    async def cmd_release(interaction: discord.Interaction):
        await _run_and_reply(interaction, "홀딩해제")

    @bot.tree.command(name="손실해제", description="일일 손실한도 매매 중단 해제 (오늘 해당 시장 재발동 안 함)")
    async def cmd_loss_release(interaction: discord.Interaction):
        await _run_and_reply(interaction, "손실해제")

    @bot.tree.command(name="분석", description="종목 분석 결과 상세 보기")
    @app_commands.describe(code="종목코드 (예: 005930, SOFI)")
    async def cmd_analyze(interaction: discord.Interaction, code: str):
        await interaction.response.send_message(f"**{code} 분석 시작...** (최대 3분 소요)")

        # 백그라운드에서 분석 실행
        import threading
        def _run_analysis():
            result = _get_verbose_analysis(trading_bot, code.upper())
            # 완료 후 채널에 전송
            async def _send():
                chunks = [result[i:i+1900] for i in range(0, len(result), 1900)]
                for chunk in chunks:
                    await interaction.channel.send(f"```\n{chunk}\n```")
            asyncio.run_coroutine_threadsafe(_send(), bot.loop)

        threading.Thread(target=_run_analysis, daemon=True).start()

    @bot.tree.command(name="성과", description="[조회] 누적 실효 수익·승률·매도 패턴/타이밍·선택 alpha")
    async def cmd_performance(interaction: discord.Interaction):
        await _run_and_reply(interaction, "성과")

    @bot.tree.command(name="헬스", description="[진단] 코어·LLM·워치독 상태 요약 (즉시)")
    async def cmd_health(interaction: discord.Interaction):
        await _run_and_reply(interaction, "헬스")

    @bot.tree.command(name="점검", description="[진단] KIS·provider별 실제 호출 점검 (십수 초)")
    async def cmd_healthcheck(interaction: discord.Interaction):
        await _run_and_reply(interaction, "점검")

    @bot.tree.command(name="도움", description="[조회] 명령어 도움말 (그룹별)")
    async def cmd_help(interaction: discord.Interaction):
        await _run_and_reply(interaction, "도움")

    @bot.tree.command(name="알림채널", description="[운영] 지금 이 채널을 봇 알림 채널로 지정")
    @app_commands.default_permissions(administrator=True)
    async def cmd_set_channel(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("권한 없음 (소유자만 가능)", ephemeral=True)
            return
        ch = interaction.channel
        guild_me = interaction.guild.me if interaction.guild else None
        if guild_me is not None and not ch.permissions_for(guild_me).send_messages:
            await interaction.response.send_message(
                "이 채널에 메시지를 보낼 권한이 없습니다 — 채널 권한을 확인하세요.", ephemeral=True)
            return
        bot._alert_channel = ch
        _save_pinned_channel_id(ch.id)
        await interaction.response.send_message(
            f"알림 채널을 **#{ch.name}** (id={ch.id})로 지정했습니다. 재시작해도 유지됩니다.",
            ephemeral=True)

    @bot.tree.command(name="업데이트", description="최신 버전으로 업데이트")
    @app_commands.default_permissions(administrator=True)
    async def cmd_update(interaction: discord.Interaction):
        if not _is_owner(interaction):
            await interaction.response.send_message("권한 없음 (소유자만 가능)", ephemeral=True)
            return
        await interaction.response.defer()
        import subprocess as _sp
        try:
            pull = _sp.run(["git", "pull"], capture_output=True, text=True, timeout=30)
            _sp.run(["sudo", "systemctl", "restart", "zusik"], timeout=10)
            await interaction.followup.send(
                f"**업데이트 완료**\n```\n{pull.stdout[:500]}\n```\n봇 재시작됨"
            )
        except Exception as e:
            await interaction.followup.send(f"**실패**: {e}")

    def _run():
        try:
            bot.run(token, log_handler=None)
        except Exception as e:
            logger.error("Discord 봇 오류: %s", e)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info("Discord 봇 시작 (슬래시 명령 지원)")


def _get_verbose_analysis(trading_bot, code: str) -> str:
    """실시간 분석 실행 + 상세 결과.

    지원 형식:
      - 암호화폐: BTC, KRW-BTC
      - 미국: SOFI (영문만)
      - 한국: 005930 (숫자 6자리)
      - 해외 (prefix): TYO:5253 (도쿄), HK:0700, SHA:600519,
                     SHE:000333, HSX:VHM, HNX:PVS, 등
        (단순 검색 용도 — 매매·스크리너 연동 없음)
    """
    # 해외 거래소 prefix: 국기·통화·소수점 자리수
    FOREIGN_META = {
        "TSE": ("", "¥", 0),      # 엔 (소수점 없음)
        "HKS": ("", "HK$", 2),
        "SHS": ("", "¥", 2),      # 위안 RMB
        "SZS": ("", "¥", 2),
        "HSX": ("", "₫", 0),      # 베트남 동
        "HNX": ("", "₫", 0),
    }
    # prefix 인식 (대소문자 무관). 모르는 prefix는 기존 US/KR 판별로 fallback.
    PREFIX_TO_KIS = {
        "TYO": "TSE", "JP": "TSE", "TSE": "TSE", "TOKYO": "TSE",
        "HK": "HKS", "HKG": "HKS", "HKEX": "HKS",
        "SHA": "SHS", "SHANGHAI": "SHS", "SSE": "SHS",
        "SHE": "SZS", "SHENZHEN": "SZS", "SZSE": "SZS",
        "HSX": "HSX", "HOSE": "HSX", "HCM": "HSX",
        "HNX": "HNX", "HAN": "HNX",
    }

    lines = [f"=== {code} 실시간 분석 ==="]
    parsed_foreign = None
    if ":" in code:
        left, right = code.split(":", 1)
        key = left.strip().upper()
        ticker = right.strip()
        if key in PREFIX_TO_KIS and ticker:
            kis_excg = PREFIX_TO_KIS[key]
            parsed_foreign = (kis_excg, ticker, FOREIGN_META.get(kis_excg, ("", "", 2)))

    # 실시간 데이터 가져오기
    try:
        # 암호화폐 / US / KR 판별
        crypto_map = {"BTC": "KRW-BTC", "ETH": "KRW-ETH", "XRP": "KRW-XRP",
                      "SOL": "KRW-SOL", "DOGE": "KRW-DOGE", "ADA": "KRW-ADA",
                      "DOT": "KRW-DOT", "AVAX": "KRW-AVAX", "MATIC": "KRW-MATIC",
                      "LINK": "KRW-LINK", "UNI": "KRW-UNI", "ATOM": "KRW-ATOM"}
        is_crypto = parsed_foreign is None and (code.upper() in crypto_map or code.startswith("KRW-"))
        is_us = parsed_foreign is None and not is_crypto and code.isalpha()

        if parsed_foreign is not None:
            kis_excg, ticker, (flag, currency, decimals) = parsed_foreign
            df = trading_bot.client.get_us_ohlcv(ticker, kis_excg)
            price_info = trading_bot.client.get_us_current_price(ticker, kis_excg)
            price = price_info["price"]
            name = price_info.get("name", ticker)
            price_fmt = f"{price:,.{decimals}f}"
            lines.append(f"{flag} {name} ({ticker}) — {currency}{price_fmt} "
                         f"({price_info.get('change_rate', 0):+.2f}%)")
            # 외국 종목 플래그 — 아래 목표가/손절가 표시용
            is_us = False  # US 공식 처리 회피
        elif is_crypto:
            ticker = crypto_map.get(code.upper(), code.upper())
            if not ticker.startswith("KRW-"):
                ticker = f"KRW-{ticker}"
            import pyupbit
            df = pyupbit.get_ohlcv(ticker, interval="day", count=100)
            price_data = pyupbit.get_current_price(ticker)
            price = float(price_data) if price_data else 0
            name = ticker.replace("KRW-", "")
            lines.append(f"{name} — {price:,.0f}원")
        elif is_us:
            df = trading_bot.client.get_us_ohlcv(code, "NASD")
            price_info = trading_bot.client.get_us_current_price(code, "NASD")
            price = price_info["price"]
            name = price_info.get("name", code)
            lines.append(f"{name} — ${price} ({price_info.get('change_rate', 0):+.2f}%)")
        else:
            df = trading_bot.client.get_ohlcv(code)
            price_info = trading_bot.client.get_current_price(code)
            price = price_info["price"]
            name = price_info.get("name", code)
            lines.append(f"{name} — {price:,}원 ({price_info.get('change_rate', 0):+.2f}%)")

        if df is None or df.empty:
            return f"{code} 데이터 없음"

        # 기술적 지표 계산
        import pandas as pd
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).iloc[-1]

        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]

        tr = pd.concat([df["high"]-df["low"], (df["high"]-close.shift()).abs(), (df["low"]-close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]

        lines.append("")
        lines.append("── 기술적 지표 ──")
        lines.append(f"  RSI: {rsi:.1f}" + (" 과매도" if rsi < 30 else " 과매수" if rsi > 70 else ""))
        lines.append(f"  MA5: {ma5:,.2f} | MA20: {ma20:,.2f}")
        lines.append(f"  ATR: {atr:,.2f}")
        lines.append(f"  정배열: {'예' if close.iloc[-1] > ma5 > ma20 else '아니오'}")

        # 목표가/손절가
        if parsed_foreign is not None:
            _exc, _tk, (_flag, currency, decimals) = parsed_foreign
            target = round(price + atr * 2, decimals)
            stop = round(price - atr, decimals)
            t_fmt = f"{target:,.{decimals}f}"
            s_fmt = f"{stop:,.{decimals}f}"
            lines.append(f"  목표가: {currency}{t_fmt} (+{(target-price)/price*100:.1f}%)")
            lines.append(f"  손절가: {currency}{s_fmt} ({(stop-price)/price*100:.1f}%)")
        elif is_us:
            target = round(price + atr * 2, 2)
            stop = round(price - atr, 2)
            lines.append(f"  목표가: ${target} (+{(target-price)/price*100:.1f}%)")
            lines.append(f"  손절가: ${stop} ({(stop-price)/price*100:.1f}%)")
        elif is_crypto:
            target = int(price + atr * 2)
            stop = int(price - atr)
            lines.append(f"  목표가: {target:,}원 (+{(target-price)/price*100:.1f}%)")
            lines.append(f"  손절가: {stop:,}원 ({(stop-price)/price*100:.1f}%)")
        else:
            target = int(price + atr * 2)
            stop = int(price - atr)
            lines.append(f"  목표가: {target:,}원 (+{(target-price)/price*100:.1f}%)")
            lines.append(f"  손절가: {stop:,}원 ({(stop-price)/price*100:.1f}%)")

        # AI 분석
        if trading_bot.use_claude:
            lines.append("")
            lines.append("── AI 에이전트 분석 중... ──")
            try:
                trading_bot.strategy.set_stock(code, name)
                trading_bot.strategy.analyze(df)
                analysis = trading_bot.strategy.get_last_analysis()
                if analysis:
                    # 에이전트별 의견
                    details = analysis.get("analyst_details", {})
                    if details:
                        lines[-1] = "── 에이전트별 의견 ──"
                        emoji_map = {"buy": "", "long_term_buy": "", "sell": "", "hold": ""}
                        name_map = {"fundamental": "펀더멘털", "sentiment": "센티멘트",
                                    "quant": "퀀트", "generalist": "종합"}
                        for role, d in details.items():
                            sig = d.get("signal", "hold")
                            conf = d.get("confidence", 0)
                            weight = d.get("weight", 0)
                            # 전체 reasoning 표시 (이전 [:120] 제거 —.
                            # 결과 텍스트는 호출자(_send)가 1900-char 단위 코드블록으로
                            # 분할 송신하므로 여기서 사전 자르면 안 됨.
                            reason = d.get("reasoning", "") or ""
                            emoji = emoji_map.get(sig, "")
                            role_name = name_map.get(role, role)
                            sig_kr = {"buy":"매수","long_term_buy":"장기매수","sell":"매도","hold":"관망"}.get(sig, sig)
                            lines.append(f"  {emoji} {role_name} → {sig_kr} ({conf:.0%}) [가중치 {weight:.0%}]")
                            # 줄바꿈 보존 + 들여쓰기 — 여러 줄 reasoning 가독성
                            for ln in reason.splitlines() or [""]:
                                lines.append(f"     {ln}")

                    # 종합 판단
                    lines.append("")
                    sig_label = {"buy":"매수","long_term_buy":"장기매수","sell":"매도","hold":"관망"}.get(analysis.get("signal",""), analysis.get("signal",""))
                    conf = analysis.get("confidence", 0)
                    lines.append("── 종합 판단 ──")
                    conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
                    lines.append(f"  신호: {sig_label}")
                    lines.append(f"  확신도: [{conf_bar}] {conf:.0%}")
                    lines.append(f"  투자비율: {analysis.get('invest_ratio', 0):.0%}")

                    if analysis.get("target_price"):
                        lines.append(f"  AI 목표가: {analysis['target_price']:,}")
                    if analysis.get("stop_loss"):
                        lines.append(f"  AI 손절가: {analysis['stop_loss']:,}")

                    reasoning = analysis.get("reasoning", "")
                    # 종합 판단 reasoning 전체 표시 (이전 [:200] 제거 —.
                    # "[" 시작이면 이미 위 details에서 분리 표시했으므로 스킵.
                    if reasoning and "[" not in reasoning[:5]:
                        lines.append(f"  {reasoning}")
            except Exception as e:
                lines[-1] = f"── AI 분석 실패: {str(e)[:50]} ──"

        # 포지션
        if trading_bot.positions.has_position(code):
            trailing = trading_bot.positions.get_trailing_info(code)
            lines.append("")
            lines.append("── 보유 포지션 ──")
            lines.append(f"  트레일링: {'활성' if trailing['active'] else '비활성'}")
            if trailing.get("high"):
                lines.append(f"  고점: {trailing['high']:,} | 손절선: {trailing['stop_price']:,}")

    except Exception as e:
        lines.append(f"분석 오류: {e}")

    return "\n".join(lines)
