# 로컬 LLM 사용하기 (API 비용/쿼터 0)

이 봇은 분석에 외부 LLM을 씁니다. 기본 provider는 Codex CLI이고, `claude`/`agy`(Antigravity)는
폴백입니다. API 비용과 쿼터 없이 자기 컴퓨터의 로컬 모델로 돌리고 싶다면,
이 문서대로 [Ollama](https://ollama.com) 를 붙이면 됩니다.

> 운영자(CLI, API 구독자)는 설정할 필요가 없습니다. 기본값이 OFF(`local_enabled: false`)이므로
> 켜지 않으면 봇 동작은 전혀 바뀌지 않습니다. 이 문서는 "로컬 모델로만 돌리고 싶은" 사용자용입니다.

---

## 1. 무엇이 켜지나

`config.yaml: ai_providers.local_enabled: true` 로 켜면:

- 저렴한 분석(센티멘트, 퀀트, 장전/장후 리포트): 로컬 모델을 우선 씁니다 (쿼터/비용 0).
- 중요한 매매 판단(매수/매도 최종): Codex 우선, 실패하면 다른 CLI와 로컬로 폴백.
- 로컬 모델이 응답하지 않으면 자동으로 꺼지고 기존 CLI/API 로 폴백합니다(봇이 멈추지 않음).
- `claude`/`codex`/`agy` 가 하나도 없어도 로컬만으로 분석이 돌아갑니다(로컬 전용 운용 가능).

"검색 가능"이란, 로컬 모델 자체엔 웹검색이 없으므로 봇이 웹검색 결과 스니펫을 프롬프트에
주입(RAG)해 최신 정보를 참고시킨다는 의미입니다. 검색 백엔드는 아래 3번에서 고릅니다.

---

## 2. 빠른 시작

### (1) Ollama 설치 + 모델 받기

```bash
# 설치 (Linux/macOS)
curl -fsSL https://ollama.com/install.sh | sh

# 환경에 맞는 모델 받기 (예시 — 본인 머신 사양에 맞게 직접 선택, 3번 참고)
ollama pull <model>      # 예: ollama pull qwen2.5:7b

# 서버 확인 (별도 터미널에서 자동 실행됨 — localhost:11434)
curl -s http://localhost:11434/api/tags
```

### (2) 봇 설정 켜기

`config.yaml` (또는 `config.local.yaml` — `configtool.py` 권장) 에서:

```yaml
ai_providers:
  local_enabled: true
  local_model: "<ollama 에 받은 모델명>"   # 환경에 맞게 직접 — 추천 기본값 없음 (3번 참고)
  local_search_backend: "duckduckgo"      # 아래 4번 참고
```

`configtool.py` 로 한 줄씩 켜도 됩니다(로컬 오버라이드 파일에 저장, 최우선):

```bash
python3 configtool.py set ai_providers.local_enabled true
python3 configtool.py set ai_providers.local_model <받은_모델명>
```

### (3) 연결 확인

```bash
python3 main.py --healthcheck
```

`[ OK ] 로컬 LLM — <모델명> @ http://localhost:11434 — 응답 OK` 가 나오면 끝입니다.

---

## 3. 모델 고르기 (정해진 추천값 없음 — 환경에 맞게)

추천 기본 모델을 따로 정하지 않습니다. 사용자 환경(GPU 유무, RAM, CPU)이 너무 다양하기 때문입니다.
본인 머신 사양과 [Ollama 라이브러리](https://ollama.com/library)를 보고 직접 고르세요. 아래는
판단을 돕는 예시이며 권장 사양이 아닙니다. 속도 수치는 GPU 없는 6코어 CPU 기준 대략치입니다.

| 예시 모델 | 용량 | CPU 속도(대략) | 성격 |
|---|---|---|---|
| `qwen2.5:7b` | ~5GB | 분석 1건 20–30초 | 한국어와 추론 균형, 가벼움 |
| `qwen2.5:14b` | ~9GB | 분석 1건 60초+ | 판단력 높음, 느리므로 호출 빈도가 낮을 때 |
| `llama3.1:8b` | ~5GB | 7b와 비슷 | 영어 분석에 강함 (한국 종목 맥락은 qwen 계열이 유리) |

> CPU 추론은 느립니다. 봇은 모든 종목을 매 사이클마다 LLM에 보내지 않고, 로컬 quant 가 1차
> 처리한 뒤 모호한 판단 케이스에만 호출합니다. 그래서 가벼운 모델로도 실사용이 됩니다. 사이클이
> 밀리면 더 작은 모델을 쓰거나 `local_timeout` 을 높이세요. GPU 가 있다면 더 큰 모델도 쾌적합니다.

---

## 4. 검색 백엔드 (대표 추천: DuckDuckGo)

로컬 모델에 웹검색 기능을 붙이려면 검색 백엔드가 필요합니다. 셋 다 API 키가 필요 없고
새 pip 의존성 없이 `requests` 로만 호출합니다(Python 3.8 호환).

| `local_search_backend` | 특징 | 언제 |
|---|---|---|
| `duckduckgo` | 계정, 키, 컨테이너 전부 불필요. 즉시 사용 가능 | 가장 단순하게 시작할 때 |
| `searxng` | 자체호스팅 메타서치, JSON API라 안정적이고 차단 없음 | 봇이 자주 검색해 안정성이 필요할 때 |
| `none` | 검색 비활성 (프롬프트만으로 응답) | 검색이 필요 없을 때 |

### DuckDuckGo (기본)

설정만 하면 됩니다. 별도 설치 없음.

```yaml
ai_providers:
  local_search_backend: "duckduckgo"
```

> 무료에 키도 필요 없어 편하지만, 짧은 시간에 너무 자주 요청하면 레이트리밋이 걸리거나 HTML 구조
> 변경으로 결과가 비어 올 수 있습니다. 그 경우 검색 컨텍스트 없이 모델이 응답하며, 봇은 멈추지 않습니다.
> 안정성이 중요하면 SearXNG 로 전환하세요.

### SearXNG (자체호스팅, 더 안정적)

Docker 로 1개 컨테이너만 띄우면 됩니다.

```bash
docker run -d --name searxng -p 8888:8080 \
  -e "SEARXNG_BASE_URL=http://localhost:8888/" \
  searxng/searxng
# JSON 출력 허용: settings.yml 의 `search.formats` 에 json 추가 필요할 수 있음
```

```yaml
ai_providers:
  local_search_backend: "searxng"
  local_searxng_url: "http://localhost:8888"
```

### 키 기반 엔진(Brave Search API / Tavily 등)으로 확장

더 높은 품질이나 안정성이 필요하면 키 기반 검색 API를 붙일 수 있습니다. `zusik/clients/local_llm.py`
의 `web_search()` 에 백엔드 분기를 추가하고(`requests` 로 해당 API 호출, `title`/`snippet`
리스트 반환), `local_search_backend` 값에 새 이름을 매핑하면 됩니다. 키는 코드가 아니라 `.env`
(예: `BRAVE_API_KEY`)에서 읽으세요. 저장소에 키를 커밋하지 마세요.

---

## 5. 동작 방식 (요약)

- 통합 지점: `zusik/clients/claude_client.py`. 로컬은 새 provider 로 끼워져, 저렴 티어
  (`easy`/`medium`/`cheap_web`/`balanced`)는 로컬 우선, 중요 티어(`hard`/`premium`)는
  Codex 우선, 사용할 수 없으면 다른 CLI와 로컬로 폴백.
- HTTP 호출: Ollama `POST /api/generate` (스트리밍 끔). CLI 가 아니라 `requests` 로 직접 호출.
- 검색 주입: `use_web_search=True` 인 호출에서 `web_search()` 결과를 프롬프트 상단에 붙입니다.
- 비용 집계 제외: 로컬 호출은 `data/api_costs.json` 의 일일 한도(total 캡)에 잡히지 않습니다.
  비용이 0이므로, 로컬을 많이 써도 유료 provider 한도를 잠식하지 않습니다.

---

## 6. 문제 해결

| 증상 | 확인 |
|---|---|
| 헬스체크에 "로컬 LLM 무응답" | `ollama serve` 실행 중인지, `local_endpoint` 포트(11434) 맞는지 |
| "연결됐으나 응답 실패" | 모델을 받았는지: `ollama pull <local_model>` |
| 분석이 너무 느림 | 더 작은 모델(`qwen2.5:7b`)로, `local_timeout` 상향, 또는 GPU 머신 |
| 검색 결과가 비어옴 | `duckduckgo` 레이트리밋일 수 있음 → `searxng` 로 전환 |

회귀 테스트: `tests/test_bot.py` 의 `LocalLlmProviderTests` (네트워크 없이 mock 으로 라우팅, 폴백,
검색 주입 검증). 통합을 바꾸면 이 테스트로 회귀를 잡으세요.
