# 설치 및 실행 가이드

zusik 을 처음부터 구동하기까지의 전체 절차입니다. 순서대로 따라 하세요.

---

## 1. 사전 요구사항

| 항목 | 요구 |
|------|------|
| Python | 3.8 이상 (3.8~3.13 CI 검증) |
| OS | Linux / macOS / WSL (systemd 서비스는 Linux) |
| 계정 | 한국투자증권(KIS) 계좌 + OpenAPI 신청 |
| 선택 | Discord 봇, Codex CLI(권장) 또는 폴백 AI CLI(claude/agy), Upbit 키 |

> **윈도우 사용자**: 이 봇은 리눅스 환경에서 가장 잘 돌아갑니다. WSL(윈도우 안에서 리눅스를
> 쓰게 해주는 기능)을 먼저 켜세요. **PowerShell** 을 관리자 권한으로 실행한 뒤 `wsl --install`
> 을 입력하고, 재시작 후 뜨는 우분투 창에서 사용자 이름과 비밀번호를 정하면 끝입니다. 이후
> 모든 명령은 그 우분투 창에서 실행합니다. ([마이크로소프트 공식 안내](https://learn.microsoft.com/ko-kr/windows/wsl/install))

---

## 2. 설치

### 2.1 저장소 클론

```bash
git clone https://github.com/zusik-py/zusik.git
cd zusik
```

### 2.2 환경 세팅 (한 줄)

`setup.sh` 가 OS(리눅스/맥/WSL)를 감지해 **uv** 로 환경을 자동 구성합니다. **수동으로 `pip install` 할 필요 없습니다.**

```bash
./setup.sh
```

- **uv**(빠르고 안정적인 Rust 기반 Python 패키지와 환경 관리자)로 `.venv` 를 만들고 의존성을 설치합니다.
  uv 가 없으면 공식 인스톨러로 자동 설치하고, 필요하면 관리형 CPython 3.11 도 자동으로 받습니다
  (시스템 파이썬 버전과 무관). 완료 후 `source .venv/bin/activate` 로 환경을 활성화하세요.
- uv 없이 표준 `python -m venv` 로만 하려면: `./setup.sh --venv`
- 운영 서버(systemd, `/usr/bin/python3`)에 설치하려면: `./setup.sh --system`
- 마지막에 `tests/test_bot.py` 게이트로 자동 검증합니다. 멱등(재실행 안전).

환경 세팅 뒤 **대화형 설정 마법사**가 이어집니다. 위험 고지 동의, `.env` 생성, KIS 키 입력,
메신저(Discord/Telegram/Slack) 선택, Upbit(선택) 순으로 진행하며 각 단계는 건너뛸 수 있습니다.
마법사만 다시 실행: `./setup.sh --config`, 마법사 없이 환경만: `./setup.sh --no-config`.
(아래 3~6장의 `.env` 항목을 직접 채워도 됩니다.)

> 수동으로 의존성만 다시 설치하려면 (uv 환경): `uv pip install -r requirements.txt`.
> 공급망 변조를 막으려면 해시 핀 설치(9.1 참고): `uv pip install --require-hashes -r requirements.lock`
> (`--venv`/`--system` 등 uv 없이 구성했다면 `uv` 를 빼고 `pip` 로 동일하게 실행하면 됩니다.)

---

## 3. 한국투자증권(KIS) OpenAPI 설정

> **브로커 선택**: 이 봇은 `.env` 의 `BROKER` 값으로 증권사를 고릅니다(기본 `kis`). 검증된 곳은
> 한국투자증권(KIS)과 **토스(`toss`)** 입니다. KIS 는 그대로 두고 아래를 따라가면 됩니다. **토스는
> 라이브 검증됐으나 모의 샌드박스가 없어 주문이 기본 dry-run** 이고(`TOSS_LIVE_ORDERS=true` 시 실주문),
> 별도 동의 없이 `BROKER=toss` 로 바로 씁니다. 지원 브로커는 kis·toss 두 곳입니다. 자세한 현황은
> README "지원 브로커" 참고.

### 3.1 앱 키 발급

1. [KIS Developers](https://apiportal.koreainvestment.com) 접속 → 로그인
2. **OpenAPI 신청** → 앱 등록
3. **App Key** / **App Secret** 발급
4. 계좌번호(앞 8자리)와 상품코드(보통 `01`) 확인

### 3.2 환경변수(.env) 설정

예시 파일을 복사해 본인 값으로 채웁니다.

```bash
cp .env.example .env
```

```ini
# 필수
KIS_APP_KEY=발급받은_앱키
KIS_APP_SECRET=발급받은_앱시크릿
KIS_ACCOUNT_NO=00000000        # 계좌번호 앞 8자리
KIS_ACCOUNT_PROD=01            # 상품코드
KIS_VIRTUAL=true               # 처음엔 반드시 true (모의투자)
KIS_API_MATURE=false           # 계좌 개설 3일 경과 시 true
```

> 주의: `.env` 는 `.gitignore` 로 제외됩니다. **시크릿을 코드나 커밋에 절대 넣지 마세요.**

---

## 4. AI 분석 설정 (선택)

LLM 분석을 사용하려면 Codex CLI를 준비하는 것을 권장합니다. Codex가 모든 분석 티어의 기본이며,
한도 도달·로그인 만료·응답 실패 때만 설치된 다른 provider로 자동 폴백합니다.

- **Codex CLI (권장)**: `setup.sh`가 설치를 먼저 안내합니다. 설치 후 `codex login`을 한 번 실행하세요.
- **폴백 (선택)**: `claude` / `agy`(Antigravity) 또는 로컬 LLM(Ollama)을 추가하면 Codex를
  사용할 수 없을 때 분석을 이어갑니다. Claude CLI는 기본 설치 대상이 아닙니다.

**요금제·개수에 맞춘 한도 설정**: 사람마다 요금제(Claude Pro/Max, Codex Plus/Pro 등)와 보유 CLI 개수가
다릅니다. `setup.sh` 의 **AI 요금제 마법사**가 설치된 CLI를 감지해 요금제를 묻고, 그에 맞는 하루 호출
한도(`api_cost.daily_limits`)와 안 쓰는 provider 차단(`ai_providers.disable_*`)을 `config.local.yaml` 에
자동 설정합니다(쿼터/요금 보호). 프리셋·수동 설정법은 [CONFIGURATION.md §9.1](CONFIGURATION.md) 참고.

AI 없이도 로컬 전략(adaptive/momentum 등)만으로 동작합니다. API 비용 0 으로 자체 머신에서 돌리려면
로컬 LLM(Ollama)도 가능합니다 → [docs/LOCAL_LLM.md](LOCAL_LLM.md).

---

## 5. Discord 봇 설정 (선택)

알림과 슬래시 명령(원격 제어)을 쓰려면 설정합니다.

### 5.1 애플리케이션과 봇 생성

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. 좌측 **Bot** → **Add Bot**

### 5.2 인텐트와 토큰

3. **Privileged Gateway Intents** → **Message Content Intent** 켜기
4. **Reset Token** → 토큰 복사 → `.env` 에 `DISCORD_BOT_TOKEN=...` 추가
5. `.env` 에 `DISCORD_OWNER_ID=본인_디스코드_유저id` 추가

### 5.3 초대

6. **OAuth2 → URL Generator**: 스코프 `bot` + `applications.commands`
7. 권한: `Send Messages`, `Read Messages/View Channels`
8. 생성된 URL 로 서버 초대 (슬래시 자동완성은 캐시로 최대 1시간 소요)

### 5.4 명령 예시

| 명령 | 설명 |
|------|------|
| `/상태` | 포트폴리오 + 손익 분해 |
| `/분석 005930` | 종목 AI 분석 |
| `/종목 목록` | watch list 조회 |
| `/종목 추가 005930`, `/종목 삭제` | watch list 편집 |

CLI 로도 보낼 수 있습니다: `python3 scripts/cmd.py 상태`

### 5.5 Telegram (선택, 알림 + 명령)

폰으로 알림받고 원격 명령까지 쓰려면 Telegram 이 가장 간단합니다.

1. Telegram 에서 **[@BotFather](https://t.me/BotFather)** → `/newbot` → 안내대로 이름 지정 → **봇 토큰** 발급
2. 만든 봇과 대화방을 열고 아무 메시지나 한 번 보냄
3. **chat id 확인**: 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열기 → `"chat":{"id":...}` 값
4. `.env` 에 추가 (또는 `./setup.sh --config` → '자세히'):

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...      # BotFather 토큰
TELEGRAM_CHAT_ID=987654321            # 위 getUpdates 의 chat.id
```

> 명령은 지정한 `chat_id` 에서 온 메시지만 처리합니다. 매수/매도 같은 거래 명령을 폰에서
> 보낼 수 있으므로, Telegram 봇 토큰과 계정을 안전하게 관리하세요(탈취 시 원격 매매 위험).

### 5.6 Slack (선택)

알림만 필요하면 Incoming Webhook 만 설정하면 됩니다. 여기에 명령 수신까지 더하려면
Socket Mode 를 켜고 app-level token 을 추가합니다(공개 URL 이 필요 없습니다).

**알림 (Incoming Webhook)**

1. **[Slack API → Your Apps](https://api.slack.com/apps)** → **Create New App** → *From scratch*
2. **Incoming Webhooks** 켜기 → **Add New Webhook to Workspace** → 채널 선택 → **Webhook URL** 복사
3. `.env` 에 추가:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxxx
```

**명령 수신 (선택, Socket Mode)**

폰이나 데스크톱 Slack 에서 봇에 명령을 보내려면 추가로 설정합니다. Telegram 과 같은 방식으로,
받은 명령은 `commands.json` 큐를 거쳐 처리되고 응답은 위 Webhook 채널로 돌아옵니다.

4. **Socket Mode** 켜기 → app-level token 생성(scope `connections:write`) → `xapp-...` 복사
5. **Slash Commands** 에서 `/zusik` 명령 추가(또는 **Event Subscriptions** 의 `message.im` 으로 DM 수신)
6. `.env` 에 추가:

```bash
SLACK_APP_TOKEN=xapp-1-...        # Socket Mode app-level token (명령 수신용)
```

> 명령으로 매수/매도 같은 거래를 보낼 수 있으므로 토큰을 안전하게 관리하세요(탈취 시 원격 매매 위험).
> `/zusik 상태` 처럼 슬래시 뒤에 명령을 붙이거나, 봇 DM 으로 `상태` 를 보내면 됩니다.

### 5.7 동시 사용

세 메신저는 **함께 쓸 수 있습니다.** 설정한 백엔드 전부로 알림이 동시 발송되며(`MultiNotifier`),
하나가 죽어도 나머지로 계속 발송됩니다. 아무것도 설정 안 하면 Discord 봇 채널 폴백으로 동작합니다.

> 셋업 후 `python3 main.py --healthcheck` 의 **메신저** 항목에서 어느 백엔드가 잡혔는지 확인하세요.

---

## 6. 암호화폐 (Upbit, 선택)

```ini
UPBIT_ACCESS_KEY=...
UPBIT_SECRET_KEY=...
```

---

## 7. 실행

### 7.1 직접 실행

> `setup.sh` 로 만든 환경을 먼저 활성화하세요 (`source .venv/bin/activate`).

```bash
python3 main.py --status     # 포트폴리오 상태 확인
python3 main.py --report     # AI 분석 리포트 (매매 안 함)
python3 main.py --once       # 단일 사이클 1회
python3 main.py              # 24/7 자동 매매
```

### 7.2 systemd 서비스 (백그라운드 영구 실행, Linux)

```bash
./deploy/setup_service.sh           # 서비스 설치
sudo systemctl start zusik   # 시작
sudo systemctl status zusik  # 상태
journalctl -u zusik -f       # 라이브 로그
```

> 서비스는 기동 전에 `tests/test_bot.py`(ExecStartPre)를 실행합니다. **테스트가 하나라도
> 실패하면 봇이 뜨지 않습니다** — 의도된 안전장치입니다.

---

## 8. 동작 확인

```bash
python3 tests/test_bot.py          # 전체 테스트 통과 확인
python3 main.py --status     # 잔고와 포지션이 정상 조회되는지 확인
```

모의투자에서 `--once` 로 한 사이클을 돌려본 뒤, 로그와 알림이 정상인지 확인하고
실거래로 전환하세요.

---

## 9. 보안 (권장)

공급망 변조, 악성코드 삽입, 코드 무단 변경을 막는 도구가 포함돼 있습니다.

### 9.1 의존성 해시 검증 설치

`requirements.lock` 에 핀된 sha256 해시로 설치하면 변조된 패키지 설치를 차단합니다.

```bash
uv pip install --require-hashes -r requirements.lock   # uv 없이라면 uv 빼고 pip
```

### 9.2 코드 무결성 트립와이어

`security_manifest.json` 기준선과 디스크 코드를 비교해 무단 변경을 탐지합니다.

```bash
python3 security_lock.py verify       # 변조 검사 (정상=exit 0, 변조=exit 1)
python3 security_lock.py generate     # 정식 코드 변경/배포 후 기준선 갱신 (커밋)
```

- 봇은 **시작 시 자동 검증**하여 위반 시 CRITICAL 로그 경보를 남깁니다.
- `SECURITY_STRICT=true` (.env) 면 위반 시 **시작을 중단**합니다.
- 정식으로 코드를 바꾼 뒤에는 `generate` 로 기준선을 갱신하고 커밋하세요.

---

## 다음 단계

- 매매 파라미터 조정: [CONFIGURATION.md](CONFIGURATION.md)
- 내부 동작 이해: [ARCHITECTURE.md](ARCHITECTURE.md)
