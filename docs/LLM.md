# LLM 분석 계약과 가중치 하네스

이 문서는 봇이 투자 판단에 LLM을 어떻게 쓰는지 정리한다. 어떤 provider(codex, agy, claude)를
쓰든 같은 형식으로 답하게 만들고, 그 답을 매매 가중치에 일관되게 반영하는 것이 목표다.

## 왜 계약이 필요한가

provider마다 말투와 장황함이 다르다. 같은 종목을 물어도 어떤 모델은 표를 그리고 어떤 모델은
문단으로 답한다. 그대로 두면 결과를 비교하거나 신뢰하기 어렵다. 그래서 모든 분석은 **하나의
출력 계약**을 따른다. 단일 진실 원천은 `zusik/analysis/claude_analyst.py` 의
`ANALYST_RESPONSE_FORMAT` 이고, 모든 애널리스트 프롬프트가 이 상수를 끼워 넣는다.

## 출력 계약 (모든 provider 공통)

분석은 다른 텍스트 없이 아래 JSON만 반환한다.

```json
{
  "signal": "buy | long_term_buy | sell | hold",
  "confidence": 0.0,
  "invest_ratio": 0.0,
  "target_price": 0,
  "stop_loss": 0,
  "reasoning": "판단 근거 3-5문장",
  "long_term_reason": "장기투자 사유 (long_term_buy 일 때만)",
  "alternative_pick": "hold 일 때 대신 살 종목 (예: 005930 삼성전자)"
}
```

- `confidence` 와 `invest_ratio` 는 0~1. 이 둘이 가중치 하네스의 입력이다.
- 응답이 JSON이 아니거나 비면 실패로 보고 다음 provider로 폴백한다. 모두 막히면 봇은 LLM 없이
  로컬 퀀트 전략으로 매매를 이어간다(분석이 멈춰도 매매는 멈추지 않는다).

## 멀티 애널리스트와 성과 가중

한 번의 판단은 네 관점이 각자 위 JSON으로 답한 뒤 투표로 합친다.

- 관점: 펀더멘털, 센티멘트, 퀀트, 종합 (`claude_analyst.py`)
- 각 애널리스트는 과거 적중률로 가중된다. 10건 이상 쌓이면 가중치는
  `max(0.5, min(2.0, 0.5 + 적중률 × 1.5))` 범위에서 움직인다. 자주 맞히는 관점의 표가 더 무겁다.
- 합산 confidence 는 `가중치 × confidence` 의 평균이다. 개별 결과는 `analyst_details` 로 남아
  합의 정도를 따질 때 쓰인다.

## 분석 결과 → 투자 가중치 하네스

confidence와 합의는 "얼마를 살지"로 이어진다. 핵심은 `bot_sizing.py` 의
`_dynamic_invest_ratio(base_ratio, conf, ...)` 다.

confidence가 사이즈로 바뀌는 1차 식:

```
conf_mult = 0.7 + (conf - 0.5) × 1.2
# confidence 0.5 → 0.7배,  0.75 → 1.0배,  1.0 → 1.3배
```

여기에 상황별 배수가 곱해진다.

```
mult = conf_mult           # LLM 확신도
     × regime_mult         # 하락 국면 점수(bear)로 축소
     × pat_mult            # 최근 30일 매도 패턴 성과 [0.85, 1.25]
     × dd_mult             # 드로우다운으로 축소 [0.5, 1.0]
     × kelly_mult          # Monte Carlo 통계 기반 Half-Kelly
     × vol_scalar          # 변동성 타겟팅
     × ai_mult             # 교차 신호 편향 [0.7, 1.3]
최종 clamp: max(0.2, min(1.5, mult))
```

합의는 별도 층으로 더 얹는다(`_consensus_invest_boost`). 네 관점이 만장일치면 크게, 우세합의면
중간, 분열이면 작게 키운다. 마지막으로 `RewardEngine.get_invest_multiplier` 가 전략·종목별
과거 성과로 한 번 더 조정한다.

최종 매수액은 이렇게 정해진다.

```
adj_ratio = _dynamic_invest_ratio(base, conf, ...)   # 위 배수들의 곱
invest    = 총자산 × adj_ratio        # 현금 한도로 캡 (bot_kr.py / bot_us.py)
invest    = invest × reward_multiplier
qty       = positions.plan_buy(...)   # 분할 매수 계획으로 수량 환산
```

즉 LLM이 강하게 확신할수록(그리고 그 관점이 과거에 잘 맞았을수록) 더 많이 산다. 약하거나
분열이면 사이즈가 줄고, hold면 사지 않는다.

## 자가학습 루프

판단 기준 자체도 실거래 결과로 보정된다.

- **보유 바닥(hold floor)**: 손실 컷이 조기였는지 정당했는지를 사후데이터(`net_if_held`)로 측정해
  비핵심 종목의 손실 보유 바닥을 조정한다. 하드스톱(-15%)은 건드리지 않는다.
  (`loss_learning.learn_hold_floor`, `_learned_hold_floor`)
- **수익 사다리·본전 giveback**: 다년 일봉 walk-forward 백테스트로 청산 파라미터를 고른다.
  (`scripts/calibrate_from_history.py` → `learned_params.json`)
- **인버스 빠른익절 임계**: 고정 1.5%가 아니라 `inverse_take` 거래의 사후 회복을 보고 올리거나
  내린다. 판 뒤에도 더 갔으면 임계를 올려 더 들고, 되돌려졌으면 내려 더 빨리 챙긴다.
  `[0.5%, 3.5%]` 로 묶는다. (`loss_learning.learn_inverse_quick_profit`,
  `_learned_inverse_quick_profit`, config `inverse.learning_enabled`)

## provider 일관성 정리

- 출력 형식: `ANALYST_RESPONSE_FORMAT` 하나로 통일. provider가 달라도 같은 JSON.
- 주 provider: `ai_providers.primary_provider: codex`가 기본이다. `premium`, `hard`, `balanced`,
  `medium`, `easy`, `cheap_web` 모두 Codex를 먼저 호출한다.
- 모델 계약: Codex는 `gpt-5.6-sol`·`low` reasoning을 기본으로 명시하고, Claude 폴백은 CLI의
  최신 역할 별칭 `opus`/`sonnet`/`haiku`를 사용한다. Claude 호출은 `--safe-mode`·비영속 text
  출력으로 격리한다.
- 폴백: Codex가 한도·로그인·응답 문제로 실패하면 agy/Claude로 넘어가고, 모두 막히면 로컬 퀀트로
  매매를 유지한다. 기존 `ClaudeClient`·`ClaudeAnalyst` 이름은 저장 데이터와 import 호환을 위해 유지한다.
- 권한 격리: Codex는 빈 임시 작업 디렉터리에서 읽기 전용 sandbox와 ephemeral 세션으로 실행한다.
  투자 분석이 소스나 설정을 바꿀 수 없다.
- 로컬 우선: `local_enabled: true`를 명시한 사용자는 저렴 티어를 로컬 LLM으로 돌릴 수 있다
  (`docs/LOCAL_LLM.md`).
