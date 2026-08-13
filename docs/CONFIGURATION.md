# 설정 가이드 (`config.yaml`)

`config.yaml` 은 매매 동작을 조정하는 파라미터 파일입니다. **시크릿이 아니며**(API 키는
`.env`), 기본값이 있으므로 그대로 시작해도 됩니다.

---

## 0. 설정 변경 방법: `scripts/configtool.py` (권장)

`config.yaml` 을 직접 편집하지 말고 **`scripts/configtool.py`** 로 변경하세요. 바꾼 값은
`config.local.yaml`(로컬, gitignore)에 따로 저장되고, `config.yaml`(주석 보존 기본값)은
그대로 유지됩니다. 봇은 시작할 때 둘을 깊은 병합합니다.

```bash
python3 configtool.py show                          # 효과적 설정 + 오버라이드 보기
python3 configtool.py get  risk.daily_loss_limit    # 효과적 값 조회
python3 configtool.py set  risk.daily_loss_limit -20000     # 오버라이드 설정
python3 configtool.py set  position.buy_tranches '[0.4, 0.3, 0.3]'
python3 configtool.py unset risk.daily_loss_limit   # 오버라이드 제거
```

- 점(`.`) 경로로 중첩 키 접근, 값은 YAML 로 타입 자동 추론(int/float/bool/list/str)
- 변경 시 자동 백업(`config.local.yaml.bak`), `config.yaml` 원본 무변경
- **재시작해야 적용**됩니다: `sudo systemctl restart zusik`

---

## 1. 설정 우선순위

값은 아래 순서로 덮어쓰며, 뒤로 갈수록 우선합니다.

1. `trading_mode.apply_mode()`: 자산 티어별 기본값
2. `performance_trainer.apply_adjustments()`: 누적 성과(merit) 기반 조정
3. `config.yaml` 의 명시값
4. `data/learned_params.json`: 다년 백테스트 학습 결과(있을 때만, 화이트리스트 키)
5. `config.local.yaml`: 사용자 로컬 오버라이드(`scripts/configtool.py` 관리, **최우선**)

---

## 2. 자산 모드 (티어)

```yaml
trading_mode: auto             # 자산 규모에 따라 티어 자동 선택
external_reserve: 0            # 주식계좌 밖 보유자산(원). 계좌가 전체의 일부일 때
                               # 공격성 부스트에 사용. 0이면 미사용.
```

- 티어 순서: seed / yolo / micro / aggressive / active / balanced / growth / wealth / premium
- 시장 조건(peace/tension/crisis/war)에 따라 티어가 하향 조정됩니다.

> `external_reserve` 는 **본인 자산 규모에 맞게** 설정하세요(개인정보이므로 공개 저장소에
> 실제 값을 커밋하지 않는 것을 권장).

---

## 3. 종목 스크리닝

```yaml
screening:
  enabled: true
  method: monte_carlo          # 선택 방식 (아래 표)
  kr_count: 5                  # KR 후보 수
  us_count: 5                  # US 후보 수
  style: defensive             # defensive=메가캡 우선 / aggressive=성장주
```

**`method`: 종목 선택 방식** (상황에 따라 골라 쓴다):

| 값 | 방식 | 적합 |
|------|------|------|
| `monte_carlo` | numpy 부트스트랩 MC (P(수익), 평균, VaR). 기본값 | 통계적 검증, 평상시 |
| `momentum` | 20일, 60일 모멘텀 가중 | 강세장 추격 |
| `trend` | 정배열(ma5>ma20>ma60) + 60일선 위 거리 | 추세장 |
| `low_vol` | 저변동(방어) + 약한 양의 추세 | 하락장, 고변동장 |

MC 외 방식은 시뮬이 없어 빠르다. 선택 후에는 RS 게이트와 레짐·로테이션·이벤트 틸트(`selection.*`)를 추가로 적용한다.

### 3.1 선별 틸트 (`selection`)

LLM/스크리너 후보를 로컬에서 검증하고 다시 정렬합니다. 20일 수익률이 지수에 못 미치면 제외하고(RS 게이트),
나머지는 상황에 맞게 가중합니다.

```yaml
selection:
  regime_adaptive: true     # 하락장(bear↑)=저변동 방어주 / 상승장=모멘텀 우선
  regime_bear_gate: 0.40    # bear score 이 이상이면 방어(저변동) 틸트 적용
  regime_vol_weight: 1.5    # 하락장 변동성 페널티 강도
  rotation: true            # 최근 매도 종목 디프리오리타이즈 (같은 테마 쏠림 방지)
  rotation_penalty: 0.03    # 로테이션 페널티 (RS %p 환산)
  event_boost: 0.05         # 장전 리포트 감지 활성 수혜 섹터 종목 부스트 (RS %p)
```

---

## 4. 리스크 관리

```yaml
risk:
  daily_target_profit_rate: 0.02   # 일일 목표 수익률 → 달성 시 쿨다운 모드
  daily_loss_limit: -15000         # 일일 손실 한도(총자산 비례로도 동작)
  stop_loss_per_stock: -0.15       # 종목별 하드 손절선
  defensive_mode_enabled: false    # drawdown 가드(보수화) 토글
```

### 4.1 일일 목표 쿨다운

목표를 채운 뒤 과열을 억제하는 스로틀입니다.

```yaml
cooldown:
  daily_target_min_confidence: 0.80   # 이후 일반주 매수는 확신도 80%+ 요구
  daily_target_invest_ratio: 0.50     # 포지션 크기 절반으로 축소
```

### 4.2 빠른 시장 급락 가드 (fast_fall_guard)

지수 단독 급락이나 **메가캡(NVDA/AAPL/MSFT/TSLA/AMZN)발 시장 급락**을 1~2 tick 안에 잡아
**신규 진입 전면 중단, 방어 모드, 인버스 헷지**를 발동합니다. **보유 종목은 자르지 않습니다.**
hold-floor 원칙에 따른 것으로, 빠른 손절은 과거 승률이 0%였기에 바닥투매를 피합니다. 열린 시장의 프록시만 봅니다.

```yaml
risk:
  fast_fall_guard:
    enabled: true
    index_sharp_pct: -2.5      # 지수 프록시 장중 이 % 이하 → 지수 단독 급락 (진입중단+헷지)
    megacap_drop_pct: -3.5     # 메가캡 장중 이 % 이하 + 지수 동반 하락 → '메가캡발 시장 급락'
    index_confirm_pct: -1.0    # 메가캡발 판정 시 지수 동반 하락 확인 임계(개별주 이슈 배제)
```

- `defensive_mode_enabled` 와 무관하게 항상 동작합니다. 토글은 위 `enabled` 만 사용합니다.
- 헷지(인버스 매수)는 `inverse.enabled: true`이고 `broker.derivative_etf_enabled: true`일 때만
  실제로 나갑니다. 파생ETF 미신청 계좌에서는 진입 중단과 방어까지만 작동합니다.

---

## 5. 포지션 관리

```yaml
position:
  buy_tranches: [1.0]              # 분할 매수 비중(일반주는 전량 1회)
  trailing_stop_pct: 0.15          # 고점 대비 트레일링 폭
  trailing_activate_pct: 0.07      # 트레일링 활성 수익률
  breakeven_arm_pct: 0.03          # 본전 보호 무장 시작 피크
  breakeven_giveback_cap: 0.025    # 고점 대비 반납 허용폭(피크 비례 보존)
  strong_profit_take: 0.035        # 순익 +3.5%↑ 익절 신호는 모멘텀 무시 통과
  surge_quick_profit: 0.10         # 급등 1차 익절 기준
  crash_instant_sell: -0.07        # 당일 급락 즉시 매도 임계
```

### 5.1 수익 사다리 (선택, 기본 OFF)

```yaml
position:
  profit_ladder: []                # []=비활성. 다년 백테스트가 데이터로 켜고 끔.
```

> 사다리/본전보호/물타기 같은 청산 파라미터는 `scripts/calibrate_from_history.py` 가 다년 일봉
> walk-forward 로 검증한 뒤 `data/learned_params.json` 에 기록하며, 그 값이 자동 적용됩니다.

### 5.2 급등 익절, 피라미딩, 급락 컷

```yaml
position:
  surge_limit_sell: 0.25           # 급등 2차(전량) 익절 기준
  surge_dynamic_vol_mult: 1.5      # 고변동/고ATR 시 익절 임계를 늦춰 추세 더 태움
  surge_ride_enabled: true         # 모멘텀 강하면 1차 익절 소량(트림)만, 추세 보유
  surge_ride_trim_ratio: 0.25      # 급등 라이딩 시 1차 트림 비율
  crash_from_high_sell: -0.20      # 고점 대비 급락 컷 (깊은 붕괴만)
  crash_gap_down: -0.08            # 시초 갭다운 컷
  crash_grace_catastrophic: -0.10  # 매수 직후 30분 보호 중엔 이 임계로 강화
  pyramid_trigger_pcts: [0.03, 0.07]  # 수익 +3%/+7% 도달 시 추가매수(승자 증폭) 레벨
  pyramid_add_ratios: [0.4, 0.3]      # 각 레벨 추가매수 비율
```

변동성 기반 사이징(개별 종목, 시장 레짐)도 사이징 배수에 함께 곱합니다.

```yaml
vol_sizing:                        # 종목 변동성 타겟팅 (고변동=작게)
  target_daily_vol: 0.025          # 이 변동성 = 배수 1.0 (5%→0.5x, 1.8%→1.4x)
  scalar_min: 0.5
  scalar_max: 1.4
vol_regime_buffer:                 # 시장 고변동 구간 총노출 축소(현금버퍼)
  normal_daily_vol: 0.012          # 지수 변동성 이하 → 풀 노출
  high_daily_vol: 0.030            # 이상 → throttle_floor 까지 축소
  throttle_floor: 0.6              # 고변동 시 노출 60% (≈40% 현금)
profit_taking:                     # 익절 임계 레짐 틸트
  regime_adaptive: true
  rsi_tilt: 8.0                    # 강세장 rsi_exit +8 / 약세장 -8
  profit_tilt: 0.03                # 강세장 익절 임계 +3%p / 약세장 -3%p
```

---

## 6. AI 합의 사이징

4-애널리스트 합의 정도에 따라 투자금 배수를 조정합니다.

```yaml
consensus:
  unanimous_multiplier: 1.20       # 만장일치
  majority_multiplier: 1.12        # 우세 다수결
  mixed_multiplier: 0.85           # 의견 혼재
  split_multiplier: 0.60           # 분열 → 축소
```

---

## 7. 인버스 ETF 헷지

```yaml
inverse:
  enabled: true                    # 인버스 헷지 활성
  adaptive: true                   # 상황 적응 게이트 사용
  trigger_crisis: true             # market_condition ∈ {crisis,war}(지속 위기)에서 발동
  trigger_index_crash: false       # 단발 지수 급락 진입. V반등 휩쏘 위험이라 기본 OFF
  max_ratio: 0.1                   # 총자산 대비 인버스 노출 상한
```

인버스는 시장 상황에 따라 발동한다. 지속적 거시 위기(crisis/war)에서만 헷지하며, 단발 지수 급락 진입
(`trigger_index_crash`)은 반등에 휩쏘되어 손실(−407k, 06-09/06-12)이 발생한 전례가 있어 기본 비활성이다.
사이징은 bear 점수 구간(0.50/0.65/0.80)에 비례하며 `max_ratio` 로 상한을 둔다.

---

## 8. 자가 보정(학습) 파이프라인

```bash
python3 calibrate_from_history.py --days 900
```

- 실거래 종목 유니버스의 다년 일봉(디스크 캐시)으로 청산 파라미터를 walk-forward 검증한다.
- 검증을 통과한 후보만 `data/learned_params.json` 에 기록한다.
- 봇 재시작 시 `load_config()` 가 화이트리스트 키만 `position` 에 최종 오버레이한다.

> 운영자 수동 실행이며 서비스 기동과는 무관합니다. cron 으로 주기 학습을 걸 수도 있습니다.

---

## 9. AI 사용량 & 비용

LLM 호출 비용을 세 가지 축으로 조절합니다. 주 provider와 폴백, 웹검색 범위, 일일 한도입니다.

```yaml
ai_providers:
  primary_provider: codex        # 모든 LLM 티어의 기본. codex | agy | claude
  disable_codex: false           # false여야 기본 Codex 라우팅 활성
  codex_model: gpt-5.6-sol       # 최신 품질 우선 모델
  codex_effort: low              # 지연·사용량과 판단 품질의 균형
  claude_effort: low             # 자동 분석 지연·쿼터 절감
  disable_agy: false             # agy(Antigravity): 구글 Gemini 계열 provider
strategy:
  name: auto_hybrid              # 변동성 기반 자동 전환 (adaptive ↔ claude quick/full)
  prefer_cli: true               # CLI 우선(구독 활용), 없으면 API 폴백
  use_web_search: true           # 웹검색 활성 (실데이터 조사)
  backtest_days: 120             # 모델선택 평가 표본 길이
  target_volatility: 0.02
reports:
  pre_market_enabled: true       # 장전 리포트. 매매 반영 O (_pre_market_buy_gate가 sentiment 사용)
  daily_enabled: false           # 장후 일일 리포트. 매매 반영 X (순수 보고용; 비용 절약 위해 기본 OFF)
api_cost:
  daily_budget_calls: 500
  price_change_threshold: 0.010  # 1% 미만 변동은 재분석 생략 (캐시)
  cache_ttl_calm: 90             # 변동 작으면 캐시 길게(분)
  daily_limits:                  # provider별 일일 호출 상한 (요금제 프리셋: saver/balanced/quality)
    claude_opus: 0               # 최고가 모델은 0 유지 (sonnet 폴백)
    claude_sonnet: 250
    claude_haiku: 900
    codex: 1800
    agy: 2500
    total: 7500
analysis_max_workers: 3          # 분석 병렬 워커 수
```

> 비용을 더 줄이려면 `daily_limits`를 saver 프리셋으로 낮추고 웹검색을 full 분석에만 적용하세요.
> `primary_provider: codex`와 `disable_codex: false`가 기본입니다. Codex가 막힐 때만 다른 provider를 씁니다.

### 9.1 요금제·개수에 맞춘 자동 설정 (권장)

사람마다 codex/claude/agy 의 **요금제와 보유 개수**가 다릅니다. 최고 요금제를 다 쓰는 사람도,
하나만 가볍게 쓰는 사람도 있죠. `./setup.sh` (또는 `./setup.sh --config`) 의 **AI 요금제 마법사**가
설치된 CLI 를 감지해 요금제를 묻고, 그에 맞는 `api_cost.daily_limits` 와 `ai_providers.disable_*` 를
`config.local.yaml` 에 자동으로 써 줍니다. 안 쓰는 provider 는 한도 0 + `disable` 로 막아 쿼터/요금을
보호합니다. 직접 잡고 싶으면 아래 프리셋을 참고해 `configtool.py` 로 설정하세요.

| provider | 요금제 | opus | sonnet | haiku / 호출 |
|---|---|---|---|---|
| **Claude** | Pro ($20) | 0 | 80 | 300 |
| | Max 5x ($100) — 권장 | 30 | 200 | 800 |
| | Max 20x ($200) | 120 | 500 | 2000 |
| | API 키 (종량제) | 50 | 300 | 1000 |
| **Codex** | Plus ($20) | — | — | 800 |
| | Pro ($200) | — | — | 3000 |
| **Antigravity(agy)** | 구글 Gemini 계열 | — | — | 250 |

`total` 은 켜진 provider 합의 약 1.2배로 자동 설정됩니다(최소 100). 값은 정확한 API 쿼터가 아니라
**보수적인 하루 호출 횟수 상한**이며, 한도에 닿으면 다른 provider 로 자동 폴백해 매매는 끊기지 않습니다.

```bash
# 마법사 없이 직접 설정하는 예 (Codex Plus 주 + Claude Pro 폴백)
python scripts/configtool.py set ai_providers.primary_provider codex
python scripts/configtool.py set api_cost.daily_limits.claude_opus 0
python scripts/configtool.py set api_cost.daily_limits.claude_sonnet 80
python scripts/configtool.py set api_cost.daily_limits.claude_haiku 300
python scripts/configtool.py set api_cost.daily_limits.codex 800
python scripts/configtool.py set api_cost.daily_limits.total 1500
python scripts/configtool.py set ai_providers.disable_agy true   # 안 쓰는 provider 차단
python scripts/configtool.py show                                   # 확인
```

> CLI 가 하나도 없어도 됩니다 — 로컬 LLM(아래) 또는 `ANTHROPIC_API_KEY` 로 분석을 돌릴 수 있고,
> 그마저 없으면 봇은 LLM 없이 로컬 퀀트 전략(추세/급락/트레일링)만으로 매매합니다.

### 9.2 로컬 LLM (API 비용/쿼터 없음)

API 대형 LLM 대신 자기 머신의 로컬 모델(Ollama)로 분석을 돌리고 싶다면 켭니다. **기본 OFF**이며,
CLI/API 구독자는 건드릴 필요가 없습니다. 켜면 저렴 티어는 로컬 우선, 중요 판단은 Codex 우선으로
동작하며 로컬로 폴백합니다. 검색 백엔드 결과를 프롬프트에 주입해 검색 기능도 활용할 수 있습니다.

```yaml
ai_providers:
  local_enabled: false             # true 로 켜면 로컬 모델 사용 (Ollama 필요)
  local_endpoint: "http://localhost:11434"
  local_model: "qwen2.5:7b"        # 권장: 7b(균형) / 14b(똑똑·느림) / llama3.1:8b
  local_search_backend: "duckduckgo"  # duckduckgo(대표·무키) | searxng | none
  local_searxng_url: "http://localhost:8888"
  local_search_results: 4
  local_timeout: 120               # CPU 추론은 느림. 초 단위
```

> 설치, 모델 선택, 검색 백엔드 상세는 **[docs/LOCAL_LLM.md](LOCAL_LLM.md)** 참고.

---

## 10. AI 신호 매매 반영 (`ai_signals`)

크로스시그널(美→韓 익일)과 데일리 Claude 편향을 **실제 매수 게이트와 사이징**에 반영합니다.
보여주기 전용이 아닙니다. 진입 게이트까지만 영향을 주며, 직접 매수를 트리거하지는 않습니다.

```yaml
ai_signals:
  enabled: true                  # 강세=확신도 하한 완화+증액 / 약세=하한 상향+축소 / 매도판단=차단
  freshness_hours: 30            # 이 시간 지난 신호는 무시 (장 마감 → 익일 세션 커버)
```

데이터가 없거나 만료되면 전부 **중립** 처리되어 거동이 변하지 않습니다. 자세한 흐름은 [ARCHITECTURE.md](ARCHITECTURE.md) §12를 참고하세요.

---

## 11. 진입 타이밍 가드

```yaml
open_guard:
  enabled: true
  delay_minutes: 5               # 개장 후 N분간 단순 신규 매수 보류 (시초 변동성 회피)
fast_entry:                      # AI 사이클(2분) 사이, 확인된 급등/반등을 로컬로 즉시 진입 (LLM 호출 없음)
  enabled: true
  interval_seconds: 120          # 스캔 주기 (30 미만은 30으로 클램프)
  max_new_per_scan: 1            # 스캔당 신규 진입 최대 종목 (과진입 방지)
  momentum_min: 0.6              # 급등 진입 모멘텀 하한 (높을수록 보수적)
  retry_throttle_sec: 600        # 같은 종목 재시도 최소 간격
```

`fast_entry` 는 `open_guard` 를 우회합니다. 확인된 급등·반등은 보류 대상이 아니라 즉시 진입합니다.

---

## 12. 핵심 매매 한도 & 브로커

```yaml
invest_ratio: 0.08               # 종목당 기본 투자 비중 (총자산 대비)
invest_ratio_max: 0.12           # 종목당 상한 (whitelist 핵심주는 별도 cap)
min_amount: 5000                 # KR 최소 주문 금액(원)
min_amount_usd: 5                # US 최소 주문 금액($)
_cash_reserve: 0.05              # 현금 버퍼 (전량 진입 방지)
long_term_ratio: 0.30            # 장기보유(LONG_TERM_BUY) 시그널 배정 비중
crypto_invest_ratio: 0.15        # 암호화폐 매수 시 KRW 잔고 대비 비중 (코드에서 0~1 clamp)
run_interval_minutes: 5          # run_once 주기(분)
candle_period: D                 # 분석 봉 (D=일봉)
us_enabled: true                 # 미국 매매 on/off. false면 미국 매수·매도·장전후 알림·USD 잔고/환율 조회 전부 비활성(KR/crypto만)
broker:
  derivative_etf_enabled: false  # KIS 파생ETF 선택확인서 등록 여부. false면 인버스/레버리지/선물 ETF 제외
```

> 미국 주식을 안 하면 `us_enabled: false` 로 끄세요(또는 `python scripts/configtool.py set us_enabled false`).
> 끄면 `us_stocks` 가 비워져 미국 관련 동작이 전부 멈춥니다. 단 **이미 보유 중인 미국 종목은 자동 관리(매도)도
> 멈추므로**, 보유분이 있으면 정리한 뒤 끄거나 KIS 앱에서 직접 매도하세요.

> `derivative_etf_enabled: false` 이면 **인버스 헷지가 비활성** 상태가 됩니다. 인버스 ETF가 파생상품에 해당하기 때문입니다.
> 인버스를 사용하려면 KIS 에서 파생상품 선택확인서를 등록한 뒤 `true` 로 설정하세요.

---

## 13. 사이징 학습 & 인덱스 회전

```yaml
reward:                          # 전략·종목·상황별 최근 성과를 투자금 배수에 반영 (자가 학습)
  decay_rate: 0.95               # 과거 성과 감쇠
  max_boost: 3.0                 # 배수 상한
  min_weight: 0.3                # 배수 하한
  learning_trades: 5             # 가중치 산출 최소 표본
index_follow:                    # 강세장에 지수 ETF로 회전 (개별주 부진 시 시장수익 추종)
  enabled: false
  bull_score_trigger: 0.50       # 지수 모멘텀 점수 ≥ 0.5 시 회전 활성
  min_kr_allocation: 0.30        # KR 인덱스 ETF 최소 비중
  rotation_cooldown_hours: 12    # 과회전 방지 쿨다운
```

---

## 14. 부가 설정

```yaml
arena:
  enabled: false                 # 가상 전략 경쟁(paper). 의사결정 미사용, 기본 OFF (API 절약)
  interval_minutes: 30
training:                        # 월간 공과 평가. 리스크/분산 파라미터 누적 조정
  monthly_target_rate: 0.05
  merit_boost: 0.1
  demerit_penalty: 0.1
monthly_deposit:                 # 월 정기 적립 시뮬 (개인값. config.local.yaml 권장)
  enabled: false
  amount: 0
log_level: INFO                  # INFO=평시 / DEBUG=verbose. 로그 위치는 docs/TESTING.md 참고
```

---

## 참고

- 설치/실행: [SETUP.md](SETUP.md), 테스트/헬스체크: [TESTING.md](TESTING.md)
- 내부 동작: [ARCHITECTURE.md](ARCHITECTURE.md)
