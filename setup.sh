#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# Zusik 설치 + 설정 마법사
#
#   ./setup.sh             # 환경 세팅(uv) + 대화형 .env 설정 마법사
#   ./setup.sh --config    # 설정 마법사만 (환경 세팅 건너뜀)
#   ./setup.sh --no-config # 환경 세팅만 (.env 마법사 건너뜀)
#   ./setup.sh --venv      # uv 없이 표준 python -m venv
#   ./setup.sh --system    # 운영(systemd /usr/bin/python3)에 설치
#   ./setup.sh --help
#
# OS(리눅스/맥/WSL) 자동 감지. uv 우선. 마법사는 위험 경고·동의·백엔드 선택을 안내한다.
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"
PY_VERSION="3.11"
REQ_FILE="requirements.txt"

if [ -t 1 ]; then C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else C_G=; C_Y=; C_R=; C_B=; C_0=; fi
info() { printf '%s\n' "${C_G}>${C_0} $*"; }
warn() { printf '%s\n' "${C_Y}!${C_0} $*"; }
err()  { printf '%s\n' "${C_R}x${C_0} $*" >&2; }
step() { printf '\n%s\n' "${C_B}== $* ==${C_0}"; }

# ── OS 감지 (전역) — Linux · macOS(Mac mini) · Windows WSL · 기타 ──
UNAME="$(uname -s)"
case "$UNAME" in Linux*) OS=linux ;; Darwin*) OS=macos ;; *) OS=other ;; esac
IS_WSL=no
grep -qiE "microsoft|wsl" /proc/version 2>/dev/null && IS_WSL=yes || true
OS_LABEL="$OS"; [ "$IS_WSL" = yes ] && OS_LABEL="$OS (WSL)" || true

MODE="auto"; CONFIG="ask"
for arg in "$@"; do
  case "$arg" in
    --system)    MODE="system" ;;
    --venv)      MODE="venv" ;;
    --config)    MODE="config" ;;
    --no-config) CONFIG="no" ;;
    -h|--help)   sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) err "알 수 없는 옵션: $arg (--help 참고)"; exit 2 ;;
  esac
done

[ -f "$REQ_FILE" ] || { err "$REQ_FILE 없음 — 프로젝트 루트에서 실행하세요."; exit 1; }

verify() {  # $1 = python 실행기
  step "검증 (test_bot.py 게이트)"
  if "$1" tests/test_bot.py; then info "${C_B}모든 테스트 통과 — 환경 준비 완료${C_0}"
  else err "테스트 게이트 실패 — 위 로그 확인"; return 1; fi
}

env_setup() {
  step "환경 감지"
  info "OS: ${OS_LABEL}  |  uname: $UNAME"

  if [ "$MODE" = "system" ]; then
    step "운영(system) 설치 — /usr/bin/python3 (systemd 경로)"
    local SYS_PY="/usr/bin/python3"; [ -x "$SYS_PY" ] || SYS_PY="$(command -v python3)"
    info "대상: $SYS_PY ($("$SYS_PY" --version 2>&1))"
    "$SYS_PY" -m pip install --user --upgrade pip
    "$SYS_PY" -m pip install --user -r "$REQ_FILE"
    verify "$SYS_PY"
    info "systemd 적용: sudo systemctl restart zusik"
    return 0
  fi

  if [ "$MODE" = "auto" ] && ! command -v uv >/dev/null 2>&1; then
    step "uv 미설치 → 공식 인스톨러로 설치 (astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | sh || warn "uv 자동 설치 실패 — venv 폴백"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi

  if [ "$MODE" = "auto" ] && command -v uv >/dev/null 2>&1; then
    step "uv 환경 세팅 (.venv, python $PY_VERSION)"
    info "uv: $(uv --version 2>&1)"
    uv venv .venv --python "$PY_VERSION"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    uv pip install -r "$REQ_FILE"
    verify "python"
  else
    step "venv 환경 세팅 (.venv) — 표준 python -m venv"
    local PY; PY="$(command -v python3 || true)"
    [ -n "$PY" ] || { err "python3 없음 — uv 또는 python3 설치 후 재실행"; exit 1; }
    info "python: $PY ($("$PY" --version 2>&1))"
    [ -d .venv ] || "$PY" -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -r "$REQ_FILE"
    verify "python"
  fi
  info "환경 활성화: source .venv/bin/activate"
}

# ── 설정 마법사 헬퍼 ──
set_env() {  # key value — .env 에 안전하게 기록(특수문자 OK, sed 미사용)
  local k="$1" v="$2"
  grep -v "^${k}=" .env > .env.tmp 2>/dev/null || true
  printf '%s=%s\n' "$k" "$v" >> .env.tmp
  mv .env.tmp .env
}
ask_yn() {  # prompt default(Y/N) → 0 if yes
  local p="$1" d="${2:-N}" a=""
  read -r -p "$p " a || true
  a="${a:-$d}"
  [[ "$a" =~ ^[Yy] ]]
}
ask_val() {  # prompt key [hidden]
  local p="$1" k="$2" hidden="${3:-}" v=""
  if [ -n "$hidden" ]; then read -r -s -p "  $p: " v || true; echo
  else read -r -p "  $p: " v || true; fi
  [ -n "$v" ] && set_env "$k" "$v" || true
}

run_wizard() {
  if [ ! -t 0 ]; then warn "비대화형 셸 — 설정 마법사를 건너뜁니다 (cp .env.example .env 후 직접 편집)."; return 0; fi

  step "위험 고지 (필독)"
  cat <<EOF
${C_Y}이 봇은 실제 돈으로 자동매매합니다.${C_0}
- 버그·네트워크 장애·시장 급변으로 ${C_B}큰 금전 손실${C_0}이 날 수 있습니다.
- 투자 자문이 아니며 수익을 보장하지 않습니다. 모든 위험은 본인 책임입니다(MIT, 무보증).
- ${C_B}반드시 모의투자(KIS_VIRTUAL=true)로 충분히 검증한 뒤, 잃어도 되는 소액으로 시작${C_0}하세요.
EOF
  read -r -p "위 위험을 이해했고 동의하면 'yes' 를 입력하세요: " AGREE || true
  [ "$AGREE" = "yes" ] || { warn "동의하지 않아 설정 마법사를 종료합니다."; return 0; }

  step ".env 생성"
  if [ -f .env ]; then
    ask_yn "이미 .env 가 있습니다. 값을 추가/덮어쓸까요? (y/N)" "N" || { info ".env 유지 — 마법사 종료."; return 0; }
  else
    cp .env.example .env; info ".env.example → .env 생성"
  fi

  step "설정 방식 선택"
  cat <<EOF
  ${C_B}1) 간단${C_0}  — KIS 키만 입력하면 끝 (모의투자 / 알림·암호화폐는 나중에). ${C_B}처음이면 추천${C_0}.
  ${C_B}2) 자세히${C_0} — 알림(Discord/Telegram/Slack)·암호화폐(Upbit)까지 전부 설정.
EOF
  read -r -p "선택 [1/2] (기본 1): " WMODE || true
  WMODE="${WMODE:-1}"
  [ "$WMODE" = "2" ] && info "자세히 모드" || { WMODE=1; info "간단 모드"; }

  step "브로커(증권사) 선택"
  cat <<EOF
  어느 증권사 Open API 로 매매할까요? 둘 다 라이브 검증된 지원 브로커입니다.
  ${C_B}1) kis${C_0}    한국투자증권 — 검증됨 (권장, 국내+미국, 모의투자 지원)
  ${C_B}2) toss${C_0}   토스증권 — 지원 (라이브 검증, 국내+미국. 모의 샌드박스 없음 → 주문 기본 dry-run)
EOF
  read -r -p "선택 [1/2] (기본 1): " BSEL || true
  case "${BSEL:-1}" in
    2) BROKER_CHOICE=toss ;;
    *) BROKER_CHOICE=kis ;;
  esac
  set_env BROKER "$BROKER_CHOICE"
  if [ "$BROKER_CHOICE" = toss ]; then
    info "BROKER=toss (지원). 토스는 모의 샌드박스가 없어 주문은 ${C_B}기본 dry-run${C_0} 입니다."
    info "시세·잔고로 충분히 검증한 뒤에만 .env 에 ${C_B}TOSS_LIVE_ORDERS=true${C_0} 로 실주문을 켜세요(실계좌 직행)."
  else
    info "BROKER=kis (검증된 기본 브로커)"
  fi

  # 활성 브로커의 키 변수명 (양쪽 키를 .env 에 함께 보관 — BROKER 만 바꿔 전환)
  case "$BROKER_CHOICE" in
    toss) KV=TOSS_CLIENT_ID; SV=TOSS_CLIENT_SECRET; AV=TOSS_ACCOUNT_NO; PORTAL="https://developers.tossinvest.com/docs" ;;
    *)    KV=KIS_APP_KEY;    SV=KIS_APP_SECRET;     AV=KIS_ACCOUNT_NO;  PORTAL="https://apiportal.koreainvestment.com" ;;
  esac
  step "API 키 — 필수 (BROKER=$BROKER_CHOICE)"
  info "$PORTAL 에서 키 발급 후, 아래에 ${C_B}붙여넣기만${C_0} 하세요."
  info "파일 편집(vim), 권한 설정(chmod)은 ${C_B}자동${C_0}으로 처리됩니다."
  ask_val "$KV (앱 키 / client_id)" "$KV"
  ask_val "$SV (시크릿, 입력 숨김)" "$SV" hidden
  if [ "$BROKER_CHOICE" = toss ]; then
    info "토스는 계좌(accountSeq)를 API 로 자동 탐색합니다 — 계좌번호 입력 생략."
  else
    ask_val "$AV (계좌번호 앞 8자리)" "$AV"
  fi
  if [ "$BROKER_CHOICE" = kis ]; then
    ask_val "KIS_ACCOUNT_PROD (상품코드, 보통 01)" KIS_ACCOUNT_PROD
    if ask_yn "모의투자로 시작하시겠습니까? ${C_B}강력 권장${C_0} (Y/n)" "Y"; then
      set_env KIS_VIRTUAL true; info "KIS_VIRTUAL=true (모의투자)"
    else
      warn "실거래(KIS_VIRTUAL=false)로 설정합니다 — 실제 손실 위험을 감수합니다."
      set_env KIS_VIRTUAL false
    fi
  fi

  step "매매 시장 — 미국 주식 on/off"
  if ask_yn "미국 주식도 매매할까요? 안 하면 한국(+암호화폐)만 운용합니다 (Y/n)" "Y"; then
    info "미국 매매 사용 (us_enabled 기본값 유지)"
  else
    PYBIN_US="$(command -v python || command -v python3 || true)"
    if [ -n "$PYBIN_US" ] && "$PYBIN_US" scripts/configtool.py set us_enabled false >/dev/null 2>&1; then
      info "미국 매매 비활성 (config.local.yaml: us_enabled=false) — 한국/암호화폐만 운용"
    else
      warn "configtool 실행 실패 — 환경 활성화 후 수동: ${C_B}python scripts/configtool.py set us_enabled false${C_0}"
    fi
  fi

  step "매매 시장 — 한국 주식 on/off"
  if ask_yn "한국 주식도 매매할까요? 안 하면 미국(+암호화폐)만 운용합니다 (Y/n)" "Y"; then
    info "한국 매매 사용 (kr_enabled 기본값 유지)"
  else
    PYBIN_KR="$(command -v python || command -v python3 || true)"
    if [ -n "$PYBIN_KR" ] && "$PYBIN_KR" scripts/configtool.py set kr_enabled false >/dev/null 2>&1; then
      info "한국 매매 비활성 (config.local.yaml: kr_enabled=false) — 미국/암호화폐만 운용"
    else
      warn "configtool 실행 실패 — 환경 활성화 후 수동: ${C_B}python scripts/configtool.py set kr_enabled false${C_0}"
    fi
  fi

  if [ "$WMODE" = "2" ]; then
    step "메신저 알림/명령 — 선택 (설정한 백엔드로 동시 발송)"
    if ask_yn "Discord 를 설정할까요? (y/N)" "N"; then
      info "${C_B}웹훅은 사실상 필수입니다${C_0} — 봇이 죽었을 때 '다운 알림'은 웹훅(또는 Telegram)으로만 옵니다."
      info "(봇 토큰은 in-process라 프로세스가 죽으면 못 보냅니다. 워치독/OnFailure가 웹훅으로 직접 발송)"
      ask_val "DISCORD_WEBHOOK_URL (웹훅 알림 — 권장·다운알림 필수)" DISCORD_WEBHOOK_URL
      ask_val "DISCORD_BOT_TOKEN (슬래시 명령용, 선택)" DISCORD_BOT_TOKEN
      ask_val "DISCORD_OWNER_ID (명령 허용할 본인 id, 선택)" DISCORD_OWNER_ID
      ask_val "DISCORD_CHANNEL_ID (알림 보낼 채널 id, 비우면 자동·런타임 /알림채널, 선택)" DISCORD_CHANNEL_ID
    fi
    if ask_yn "Telegram 을 설정할까요? (알림+명령) (y/N)" "N"; then
      info "@BotFather 로 봇 생성 → 토큰. chat id 는 봇에게 메시지 후 확인."
      ask_val "TELEGRAM_BOT_TOKEN" TELEGRAM_BOT_TOKEN hidden
      ask_val "TELEGRAM_CHAT_ID" TELEGRAM_CHAT_ID
    fi
    if ask_yn "Slack 알림을 설정할까요? (y/N)" "N"; then
      ask_val "SLACK_WEBHOOK_URL" SLACK_WEBHOOK_URL
      if ask_yn "Slack 명령 수신(Socket Mode)도 쓸까요? (y/N)" "N"; then
        info "Slack 앱에서 Socket Mode 켜고 app-level token(xapp-, connections:write) 발급."
        ask_val "SLACK_APP_TOKEN" SLACK_APP_TOKEN hidden
      fi
    fi

    step "암호화폐 (Upbit) — 선택"
    if ask_yn "Upbit 키를 설정할까요? (y/N)" "N"; then
      ask_val "UPBIT_ACCESS_KEY" UPBIT_ACCESS_KEY hidden
      ask_val "UPBIT_SECRET_KEY" UPBIT_SECRET_KEY hidden
    fi

    step "로컬 LLM (Ollama) — 선택 · API 비용/쿼터 0"
    info "claude/codex/agy 대신 자기 컴퓨터의 로컬 모델로 분석을 돌립니다(웹검색 주입 지원)."
    info "CLI/API 를 쓰는 분은 ${C_B}건너뛰어도 됩니다${C_0} — 기본 OFF. 자세히: docs/LOCAL_LLM.md"
    if ask_yn "로컬 LLM(Ollama)을 사용하도록 설정할까요? (y/N)" "N"; then
      local LMODEL LBACKEND PYBIN LDEFAULT="llama3.2"
      # ollama 설치 — 없으면 설치 여부 묻기 (set -e 대비 || 가드)
      if ! command -v ollama >/dev/null 2>&1; then
        if ask_yn "ollama 가 없습니다. 설치할까요? (Y/n)" "Y"; then
          info "설치 중: curl -fsSL https://ollama.com/install.sh | sh"
          curl -fsSL https://ollama.com/install.sh | sh || warn "ollama 설치 실패 — 수동: https://ollama.com/download"
        else
          warn "ollama 미설치 — 없거나 무응답이면 봇이 자동으로 CLI/API 폴백"
        fi
      fi
      # 모델 — 사람마다 다르니 묻되, 기본은 llama(가볍고 범용)
      info "로컬 모델은 사람마다 다릅니다 — ${C_B}기본은 llama${C_0}(가볍고 범용). 목록: https://ollama.com/library"
      read -r -p "  사용할 모델명 (Ollama 모델, 기본 ${LDEFAULT}): " LMODEL || true
      LMODEL="${LMODEL:-$LDEFAULT}"
      read -r -p "  검색 백엔드 duckduckgo/searxng/none (기본 duckduckgo): " LBACKEND || true
      LBACKEND="${LBACKEND:-duckduckgo}"
      PYBIN="$(command -v python || command -v python3 || true)"
      if [ -n "$PYBIN" ] && "$PYBIN" scripts/configtool.py set ai_providers.local_enabled true >/dev/null 2>&1; then
        "$PYBIN" scripts/configtool.py set ai_providers.local_model "$LMODEL" >/dev/null 2>&1 || true
        "$PYBIN" scripts/configtool.py set ai_providers.local_search_backend "$LBACKEND" >/dev/null 2>&1 || true
        info "로컬 LLM 활성 (config.local.yaml): model=$LMODEL · search=$LBACKEND"
        # ollama 있으면 지금 모델 pull 제안 (용량 커서 기본 N)
        if command -v ollama >/dev/null 2>&1 && ask_yn "  지금 '${LMODEL}' 모델을 받을까요? (ollama pull, 용량 큼) (y/N)" "N"; then
          info "받는 중: ollama pull $LMODEL (수 GB — 시간 걸릴 수 있음)"
          ollama pull "$LMODEL" || warn "pull 실패 — 나중에: ollama pull $LMODEL"
        else
          info "모델 받기(나중에): ${C_B}ollama pull $LMODEL${C_0}  →  확인: python main.py --healthcheck"
        fi
      else
        warn "configtool 실행 실패 — 환경 활성화 후 수동 설정:"
        warn "  python scripts/configtool.py set ai_providers.local_enabled true"
      fi
    fi
  else
    info "간단 모드 — 알림(Discord/Telegram/Slack)·암호화폐는 건너뜁니다."
    info "나중에 추가: ${C_B}./setup.sh --config${C_0} → '자세히' 선택 (언제든 다시 실행 가능)"
  fi

  # 다운 알림 경로 점검 — 봇이 죽으면(크래시/멈춤) 알림은 웹훅/Telegram(out-of-process)으로만 온다.
  # 봇 토큰(슬래시)은 in-process라 프로세스가 죽으면 무용지물 → 둘 중 하나는 사실상 필수.
  if ! grep -qE '^(DISCORD_WEBHOOK_URL|TELEGRAM_BOT_TOKEN)=.+' .env 2>/dev/null; then
    warn "${C_B}다운 알림 채널 미설정${C_0} — 봇이 죽거나 멈춰도 알림을 못 받습니다."
    warn "  ${C_B}DISCORD_WEBHOOK_URL${C_0} 또는 ${C_B}TELEGRAM_BOT_TOKEN${C_0} 중 하나를 꼭 설정하세요 (./setup.sh --config → '자세히')."
  fi

  chmod 600 .env 2>/dev/null || true
  info "${C_B}.env 설정 완료${C_0} (권한 600 자동 설정). 값을 바꾸려면 ${C_B}./setup.sh --config${C_0} 다시 실행 — 파일 직접 편집 불필요."
}

# ── AI CLI 설치 점검 (codex 주 · claude/antigravity 폴백) ──
# 봇은 Codex를 먼저 쓰고 한도/로그인/실패 때 설치된 다른 CLI로 폴백한다.
# set -e 대비: 모든 설치 명령은 || warn 로 가드(실패해도 스크립트 중단 안 함).
setup_ai_clis() {
  [ -t 0 ] || return 0   # 비대화형 셸이면 자동 설치 생략
  step "AI CLI 점검 (codex 주 · claude/antigravity 폴백)"
  info "봇은 Codex를 기본으로 사용하고, 사용할 수 없을 때 다른 CLI로 자동 폴백합니다."

  # 1) codex (OpenAI Codex CLI) — 기본 provider
  if command -v codex >/dev/null 2>&1; then
    info "codex — 이미 설치됨"
  elif ask_yn "codex CLI 가 없습니다. 설치할까요? (npm 필요) (Y/n)" "Y"; then
    if command -v npm >/dev/null 2>&1; then
      info "설치 중: npm install -g @openai/codex"
      npm install -g @openai/codex || warn "codex 설치 실패 — 수동: npm i -g @openai/codex"
      if command -v codex >/dev/null 2>&1; then info "codex 설치 완료 — 로그인: ${C_B}codex login${C_0}"; fi
    else
      warn "npm 미설치 — Node.js(https://nodejs.org) 설치 후: npm install -g @openai/codex"
    fi
  else
    warn "codex 건너뜀"
  fi

  # 2) claude (Claude Code) — 선택 폴백
  if command -v claude >/dev/null 2>&1; then
    info "claude — 이미 설치됨 (폴백)"
  elif ask_yn "Claude CLI를 폴백으로도 설치할까요? (y/N)" "N"; then
    info "설치 중: curl -fsSL https://claude.ai/install.sh | bash"
    curl -fsSL https://claude.ai/install.sh | bash || warn "claude 설치 실패 — 수동: https://docs.claude.com/claude-code (또는 npm i -g @anthropic-ai/claude-code)"
    if command -v claude >/dev/null 2>&1; then info "claude 설치 완료 — 최초 1회 로그인: ${C_B}claude${C_0}"; fi
  else
    info "claude 폴백 건너뜀"
  fi

  # 3) antigravity(agy) — 구글 Gemini 계열 provider. 네이티브 설치 스크립트(npm 아님).
  if command -v agy >/dev/null 2>&1; then
    info "antigravity(agy) — 이미 설치됨"
  elif ask_yn "antigravity(agy) CLI 가 없습니다. 설치할까요? (Y/n)" "Y"; then
    info "설치 중: curl -fsSL https://antigravity.google/cli/install.sh | bash"
    curl -fsSL https://antigravity.google/cli/install.sh | bash || warn "agy 설치 실패 — 수동: https://antigravity.google/docs/cli-install"
    if command -v agy >/dev/null 2>&1 || [ -x "$HOME/.local/bin/agy" ]; then
      info "agy 설치 완료 — 최초 1회 로그인: ${C_B}agy${C_0}  (PATH 에 ~/.local/bin 필요)"
    fi
  else
    warn "antigravity 건너뜀"
  fi

  # 요약 (set -e 대비 if 사용)
  local have=""
  for c in codex agy claude; do
    if command -v "$c" >/dev/null 2>&1; then have="$have $c"; fi
  done
  if [ -n "$have" ]; then info "사용 가능 AI CLI:${have}"
  else warn "AI CLI 없음 — 로컬 LLM(아래)을 켜거나 Codex CLI를 설치하세요."; fi

  # agy 로그인 영속화 — Secret Service 데몬이 없으면(WSL/헤드리스) 토큰이 안 남아 매번 재로그인.
  # gnome-keyring 등 키링이 있으면(데스크톱) 이 블록은 건너뜀. (busctl=systemd/Linux 전용)
  if command -v agy >/dev/null 2>&1 && command -v busctl >/dev/null 2>&1 \
     && [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    if ! busctl --user list 2>/dev/null | awk '{print $1}' | grep -qx "org.freedesktop.secrets"; then
      warn "agy: Secret Service 데몬이 없어 ${C_B}로그인이 매번 반복${C_0}될 수 있습니다(WSL/헤드리스)."
      if ask_yn "  로그인 영속화 fix(pass-secret-service)를 실행할까요? (y/N)" "N"; then
        bash scripts/fix_agy_auth.sh || warn "fix 실패 — 수동: bash scripts/fix_agy_auth.sh"
      else
        info "  나중에: ${C_B}bash scripts/fix_agy_auth.sh${C_0}"
      fi
    fi
  fi
}

# ── AI 요금제/개수별 호출 한도 (선택) ──
# Codex를 주로 쓰되, 사람마다 codex/claude/agy 요금제와 보유 개수가 달라 한도를 다르게 잡는다.
# 설치된 CLI 를 보고 요금제를 물어 config.local.yaml 에 api_cost.daily_limits + ai_providers.disable_*
# 를 쓴다(configtool.py). 안 쓰는 provider 는 한도 0 + disable 로 막아 쿼터/요금을 보호한다.
setup_ai_plans() {
  [ -t 0 ] || return 0
  local PYBIN; PYBIN="$(command -v python || command -v python3 || true)"
  [ -n "$PYBIN" ] || { warn "python 없음 — AI 요금제 한도 설정 건너뜀"; return 0; }
  # 설정할 CLI 가 하나도 없으면 생략
  command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1 \
    || command -v agy >/dev/null 2>&1 || return 0

  step "AI 요금제/개수별 호출 한도 (선택)"
  info "Codex 주 사용량과 폴백 provider 한도를 요금제에 맞춥니다. 안 쓰는 provider는 차단합니다."
  info "모르면 기본값(권장)으로 두세요 — 나중에 ${C_B}python scripts/configtool.py${C_0} 로 바꿀 수 있습니다."
  ask_yn "지금 AI 요금제에 맞춰 한도를 설정할까요? (Y/n)" "Y" || { info "기본 한도 유지 (config.yaml)"; return 0; }

  cset() { "$PYBIN" scripts/configtool.py set "$1" "$2" >/dev/null 2>&1 || warn "설정 실패: $1=$2"; }

  local copus=0 csonnet=0 chaiku=0 ccodex=0 cagy=0

  # 1) Claude (Anthropic) — claude CLI 또는 ANTHROPIC_API_KEY
  if command -v claude >/dev/null 2>&1 || grep -qE '^ANTHROPIC_API_KEY=.+' .env 2>/dev/null; then
    cat <<EOF
  ${C_B}Claude 요금제${C_0} (claude 감지됨):
   1) Pro (\$20)       — 가볍게 (haiku/sonnet 위주, opus 0)
   2) Max 5x (\$100)   — 권장 (opus 약간 + sonnet 넉넉)
   3) Max 20x (\$200)  — 넉넉 (opus 적극)
   4) API 키 (종량제)  — 중간 한도 (호출당 과금 주의)
   5) 안 씀
EOF
    read -r -p "선택 [1-5] (기본 2): " CP || true
    case "${CP:-2}" in
      1) copus=0;   csonnet=80;  chaiku=300 ;;
      3) copus=120; csonnet=500; chaiku=2000 ;;
      4) copus=50;  csonnet=300; chaiku=1000 ;;
      5) copus=0;   csonnet=0;   chaiku=0 ;;
      *) copus=30;  csonnet=200; chaiku=800 ;;
    esac
    cset api_cost.daily_limits.claude_opus "$copus"
    cset api_cost.daily_limits.claude_sonnet "$csonnet"
    cset api_cost.daily_limits.claude_haiku "$chaiku"
    info "Claude 한도: opus $copus / sonnet $csonnet / haiku $chaiku"
  fi

  # 2) Codex (OpenAI)
  if command -v codex >/dev/null 2>&1; then
    cat <<EOF
  ${C_B}Codex 요금제${C_0} (codex 감지됨):
   1) Plus (\$20)   — 800/일
   2) Pro (\$200)   — 3000/일
   3) 안 씀
EOF
    read -r -p "선택 [1-3] (기본 1): " XP || true
    case "${XP:-1}" in
      2) ccodex=3000 ;;
      3) ccodex=0 ;;
      *) ccodex=800 ;;
    esac
    cset api_cost.daily_limits.codex "$ccodex"
    [ "$ccodex" -gt 0 ] && cset ai_providers.disable_codex false || cset ai_providers.disable_codex true
    info "Codex 한도: $ccodex (disable=$([ "$ccodex" -gt 0 ] && echo false || echo true))"
  fi

  # 3) Antigravity(agy) — 구글 Gemini 계열 provider
  if command -v agy >/dev/null 2>&1; then
    if ask_yn "  Antigravity(agy)를 분석에 쓸까요? (Y/n)" "Y"; then
      cagy=250; cset ai_providers.disable_agy false
    else
      cagy=0;   cset ai_providers.disable_agy true
    fi
    cset api_cost.daily_limits.agy "$cagy"
    info "Antigravity(agy) 한도: $cagy"
  fi

  # 4) total — 켜진 provider 합(여유 20%) · 최소 100 보장
  local sum=$((copus + csonnet + chaiku + ccodex + cagy))
  local total=$(( sum + sum / 5 ))
  [ "$total" -lt 100 ] && total=100
  cset api_cost.daily_limits.total "$total"
  info "${C_B}AI 한도 설정 완료${C_0} (config.local.yaml) · 일일 total $total · 확인: ${C_B}python scripts/configtool.py show${C_0}"
}

# ── git 훅 설치 (무결성 기준선 자동 갱신) ──
# .py/config.yaml 커밋 시 security_manifest.json 을 자동 재생성해 함께 커밋 → 재시작 시 무결성 경고 방지.
install_git_hooks() {
  command -v git >/dev/null 2>&1 || return 0
  git rev-parse --git-dir >/dev/null 2>&1 || return 0   # git repo 아니면 생략
  [ -f scripts/hooks/pre-commit ] || return 0
  chmod +x scripts/hooks/pre-commit 2>/dev/null || true
  git config core.hooksPath scripts/hooks
  info "git pre-commit 훅 활성화 (core.hooksPath=scripts/hooks) — 커밋 시 무결성 기준선 자동 갱신"
}

# ── 디렉터리 준비 (로그/상태) ──
make_dirs() {
  step "디렉터리 준비 (logs/ · data/)"
  mkdir -p logs data
  chmod 700 data 2>/dev/null || true   # 토큰·상태 파일은 소유자 전용(시크릿 보호)
  info "logs/ (로그) · data/ (상태·토큰, 권한 700) 준비 완료"
}

# ── 셋업 직후 헬스체크 (실제 연결 점검) — KIS 키가 있고 대화형일 때만 ──
offer_healthcheck() {
  [ -t 0 ] || return 0
  grep -qE '^KIS_APP_KEY=.+' .env 2>/dev/null || return 0
  printf '\n'
  ask_yn "지금 헬스체크로 실제 연결(KIS/AI/메신저)을 확인할까요? ${C_B}권장${C_0} (Y/n)" "Y" || return 0
  python main.py --healthcheck || warn "헬스체크 경고 — 위 메시지의 해결 힌트를 확인하세요."
}

# ── 상황별 첫 설정 안내 ──
first_time_guide() {
  local KV HASMSG
  KV="$(grep -E '^KIS_VIRTUAL=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
  HASMSG=no
  grep -qE '^(DISCORD_(WEBHOOK_URL|BOT_TOKEN)|TELEGRAM_BOT_TOKEN|SLACK_WEBHOOK_URL)=.+' .env 2>/dev/null && HASMSG=yes || true

  printf '\n%s\n' "${C_B}처음이신가요? 상황별 다음 단계${C_0}:"
  if [ ! -f .env ] || ! grep -qE '^KIS_APP_KEY=.+' .env 2>/dev/null; then
    printf '  %s\n' "[KIS 키 아직 없음]  1) https://apiportal.koreainvestment.com 가입 → 앱 키 발급"
    printf '  %s\n' "                    2) ./setup.sh --config 로 .env 입력 (모의투자 강력 권장)"
  elif [ "$KV" = "true" ]; then
    printf '  %s\n' "[모의투자 — 안전]   가짜 돈으로 충분히 검증하세요. 익숙해지면 실거래 전환."
  else
    printf '  %s\n' "${C_Y}[실거래 — 실제 돈]${C_0}  잃어도 되는 소액만. 며칠 모의투자로 검증 후 권장."
  fi
  [ "$HASMSG" = no ] && \
    printf '  %s\n' "[알림 미설정]       폰으로 매매/경고 받으려면 ./setup.sh --config 에서 Discord/Telegram 설정."
  printf '  %s\n' "공통:  source .venv/bin/activate  →  python main.py --healthcheck  →  python main.py --status"
  printf '  %s\n' "문서:  docs/SETUP.md (설치) · docs/TESTING.md (점검/테스트) · docs/LOCAL_LLM.md (로컬 LLM) · README.md"
}

# ── 24/7 가동 + OS별 상시 실행 안내 ──
always_on_guide() {
  local keep
  case "$OS" in
    linux) keep="systemd 서비스로 등록하면 부팅 시 자동 시작 + 죽으면 자동 재시작:
       ./setup.sh --system   그리고   bash deploy/setup_service.sh" ;;
    macos) keep="Mac mini 등 항상 켜두고 절전 끄기(시스템 설정 > 에너지). 상시 가동:
       caffeinate -is python main.py   (또는 launchd plist 등록)" ;;
    *)     keep="컴퓨터를 끄지 말고 상시 가동: nohup python main.py >/dev/null 2>&1 &
       (가능하면 서비스/스케줄러로 자동 재시작 구성)" ;;
  esac
  cat <<EOF

${C_B}24/7 가동 (중요)${C_0}:
  이 봇은 장 시간 내내 ${C_B}계속 켜져 있어야${C_0} 합니다 — 노트북을 닫거나 끄면 매매가 멈춥니다.
  지원 OS: Linux · macOS(Mac mini) · Windows ${C_B}WSL${C_0}  (Windows 기본 cmd/PowerShell 미지원 → WSL 사용)
  - ${keep}
EOF
  [ "$IS_WSL" = yes ] && \
    printf '  %s\n' "${C_Y}WSL 주의:${C_0} Windows 절전/종료 시 WSL도 멈춥니다 — 항상 켜지는 PC·서버·Mac mini 권장."
  printf '\n  %s\n' "셋업이 잘 됐는지 확인:  python main.py --healthcheck"
}

# ── 실행 ──
make_dirs
install_git_hooks
if [ "$MODE" = "config" ]; then
  setup_ai_clis
  setup_ai_plans
  run_wizard
else
  env_setup
  setup_ai_clis
  setup_ai_plans
  if [ "$MODE" = "system" ]; then always_on_guide; exit 0; fi
  if [ "$CONFIG" != "no" ] && [ -t 0 ]; then
    printf '\n'
    if ask_yn "지금 .env 설정 마법사를 진행할까요? (Y/n)" "Y"; then run_wizard
    else info "나중에 설정: ./setup.sh --config"; fi
  fi
  offer_healthcheck
fi

first_time_guide
always_on_guide
