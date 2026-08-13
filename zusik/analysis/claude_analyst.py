from __future__ import annotations
"""3인 경쟁 애널리스트 시스템.

3명의 독립 애널리스트가 각자 다른 관점에서 분석하고,
심판(Judge)이 성과 기반 가중치로 최종 판단을 내림.

┌──────────────────────────────────────────────────┐
│  1. 펀더멘털 애널리스트 (Fundamental)              │
│     - 공식 문서: 중앙은행 보고서, 실적 발표,       │
│       SEC/DART 공시, 정부 정책, 기관 리포트         │
│     - 국내외 모든 공식 기관 자료                    │
│                                                    │
│  2. 센티멘트 애널리스트 (Sentiment)                │
│     - 찌라시, 커뮤니티 루머, SNS 동향,             │
│       개미 심리, 공포/탐욕, 테마/키워드 급등        │
│     - 비공식 정보 종합                              │
│                                                    │
│  3. 퀀트 애널리스트 (Quant)                        │
│     - 순수 수치 분석: 가격, 거래량, 지표,          │
│       패턴 인식, 통계적 이상치, 변동성 모델         │
│     - 뉴스 없이 숫자만으로 판단                     │
│                                                    │
│  4. 종합 애널리스트 (Generalist)                   │
│     - 기존 올인원 방식: 공식 문서 + 심리 + 수치를  │
│       모두 보고 총체적으로 판단                     │
│     - 전문 분야 없이 균형 잡힌 종합                 │
│                                                    │
│  → 심판 (Judge): 4인의 분석을 성과 가중치로 합산   │
└──────────────────────────────────────────────────┘
"""

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 성과 추적 파일 ──
import os

ANALYST_PERF_FILE = os.path.join("data", "analyst_performance.json")


def _load_perf() -> dict:
    if os.path.exists(ANALYST_PERF_FILE):
        with open(ANALYST_PERF_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_perf(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(ANALYST_PERF_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════
# 지표 계산 (공통)
# ══════════════════════════════════════

def calc_indicators(df: pd.DataFrame) -> dict:
    """주요 기술적 지표 계산."""
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal

    bb_ma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_ma + 2 * bb_std
    bb_lower = bb_ma - 2 * bb_std

    daily_returns = close.pct_change().dropna()
    vol_20d = daily_returns.iloc[-20:].std() if len(daily_returns) >= 20 else None

    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    vol_ma20 = volume.rolling(20).mean()
    high_52w = high.iloc[-250:].max() if len(close) >= 250 else high.max()
    low_52w = low.iloc[-250:].min() if len(close) >= 250 else low.min()

    curr = df.iloc[-1]
    return {
        "현재가": int(curr["close"]),
        "시가": int(curr["open"]),
        "고가": int(curr["high"]),
        "저가": int(curr["low"]),
        "거래량": int(curr["volume"]),
        "이동평균_5일": int(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else None,
        "이동평균_20일": int(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else None,
        "이동평균_60일": int(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else None,
        "정배열": bool(pd.notna(ma5.iloc[-1]) and pd.notna(ma20.iloc[-1]) and pd.notna(ma60.iloc[-1]) and ma5.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1]),
        "RSI_14": round(float(rsi.iloc[-1]), 1) if pd.notna(rsi.iloc[-1]) else None,
        "MACD": round(float(macd.iloc[-1]), 1) if pd.notna(macd.iloc[-1]) else None,
        "MACD_시그널": round(float(macd_signal.iloc[-1]), 1) if pd.notna(macd_signal.iloc[-1]) else None,
        "MACD_히스토그램": round(float(macd_hist.iloc[-1]), 1) if pd.notna(macd_hist.iloc[-1]) else None,
        "볼린저_상단": int(bb_upper.iloc[-1]) if pd.notna(bb_upper.iloc[-1]) else None,
        "볼린저_하단": int(bb_lower.iloc[-1]) if pd.notna(bb_lower.iloc[-1]) else None,
        "20일_변동성": round(float(vol_20d), 4) if vol_20d is not None else None,
        "ATR_14": int(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
        "거래량_20일평균": int(vol_ma20.iloc[-1]) if pd.notna(vol_ma20.iloc[-1]) else None,
        "거래량_비율": round(float(curr["volume"] / vol_ma20.iloc[-1]), 2) if pd.notna(vol_ma20.iloc[-1]) and vol_ma20.iloc[-1] > 0 else None,
        "52주_고가": int(high_52w),
        "52주_저가": int(low_52w),
        "52주_고가대비": round((float(curr["close"]) / float(high_52w) - 1) * 100, 1),
    }


def summarize_price_action(df: pd.DataFrame, days: int = 10) -> str:
    recent = df.tail(days)
    lines = []
    for _, row in recent.iterrows():
        d = row.name.strftime("%m/%d") if hasattr(row.name, "strftime") else str(row.name)
        chg = ((row["close"] - row["open"]) / row["open"]) * 100
        lines.append(f"  {d}: 시={row['open']:>8,} 고={row['high']:>8,} 저={row['low']:>8,} 종={row['close']:>8,} ({chg:+.1f}%) 거래량={row['volume']:,}")
    return "\n".join(lines)


# ══════════════════════════════════════
# 개별 애널리스트
# ══════════════════════════════════════

ANALYST_RESPONSE_FORMAT = """\
중요 원칙:
- 이 계좌의 현금은 전액 투자해도 되는 여유자금입니다. 보수적으로 현금을 아낄 필요 없습니다.
- hold보다는 적극적으로 buy 또는 sell 판단을 내려주세요.
- 만약 이 종목이 hold라면, 대신 매수할 만한 대안 종목을 alternative_pick에 제안해주세요.

반드시 아래 JSON 형식으로만 응답. 다른 텍스트 없이 JSON만:
{{
  "signal": "buy" 또는 "long_term_buy" 또는 "sell" 또는 "hold",
  "confidence": 0.0에서 1.0,
  "invest_ratio": 0.0에서 1.0,
  "target_price": 목표가(정수),
  "stop_loss": 손절가(정수),
  "reasoning": "판단 근거 3-5문장",
  "long_term_reason": "장기투자 사유 (signal이 long_term_buy일 때만, 아니면 빈 문자열)",
  "alternative_pick": "hold일 때 대신 살 종목 (종목코드 종목명, 예: 005930 삼성전자). buy/sell이면 빈 문자열"
}}"""


class _BaseAnalyst:
    """개별 애널리스트 베이스."""

    role: str = ""
    name_kr: str = ""

    def __init__(self, client, model: str):
        self.client = client  # ClaudeClient 또는 anthropic.Anthropic
        self.model = model
        self._use_web_search = True

    def analyze(self, stock_code: str, stock_name: str, df: pd.DataFrame,
                indicators: dict, price_action: str, extra_context: str = "") -> dict:
        prompt = self._build_prompt(stock_code, stock_name, indicators, price_action, extra_context)
        return self._call(prompt)

    def _build_prompt(self, stock_code, stock_name, indicators, price_action, extra_context) -> str:
        raise NotImplementedError

    # 애널리스트별 AI 티어 배정
    _tier = "easy"  # 기본 easy (agy/codex)

    def _call(self, prompt: str) -> dict:
        try:
            from zusik.clients.claude_client import ClaudeClient
            if isinstance(self.client, ClaudeClient):
                raw = self.client.message(
                    prompt, max_tokens=800,
                    use_web_search=self._use_web_search,
                    tier=self._tier,
                )
            else:
                # 레거시: anthropic.Anthropic 직접 사용
                kwargs = {
                    "model": self.model,
                    "max_tokens": 800,        # 1200→800: 구조화 JSON 응답엔 충분, 토큰 절감
                    "messages": [{"role": "user", "content": prompt}],
                }
                if self._use_web_search:
                    kwargs["tools"] = [{"type": "web_search_20250305"}]
                response = self.client.messages.create(**kwargs)
                parts = [b.text for b in response.content if b.type == "text"]
                raw = "\n".join(parts).strip()

            return self._parse(raw)
        except Exception as e:
            logger.warning("%s 분석 실패: %s", self.name_kr, e)
            return {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                    "target_price": 0, "stop_loss": 0, "reasoning": f"분석 실패: {e}",
                    "long_term_reason": ""}

    @staticmethod
    def _parse(raw: str) -> dict:
        text = raw
        # ```json ... ``` 마크다운 코드블록 우선 추출
        if "```" in text:
            chunks = text.split("```")
            # 3개 이상이면 첫 번째 fenced block 사용
            if len(chunks) >= 3:
                candidate = chunks[1]
                if candidate.lstrip().lower().startswith("json"):
                    candidate = candidate.lstrip()[4:]
                text = candidate.strip()

        # 첫 `{` 부터 균형 잡힌 `}` 까지만 추출 (Extra data 방지)
        start = text.find("{")
        if start == -1:
            return {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                    "target_price": 0, "stop_loss": 0, "reasoning": "파싱 실패: { 없음",
                    "long_term_reason": ""}

        depth = 0
        end = -1
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            # 균형 못 맞추면 첫 `{` 부터 마지막 `}`까지 시도 (레거시 폴백)
            end = text.rfind("}")

        try:
            parsed = json.loads(text[start:end + 1])
            #: LLM이 "stop_loss": null 처럼 None을 넘기면 하류에서
            # None>0 / None*w TypeError 발생 (Dell _judge 크래시). 숫자 필드 None→0 정규화.
            if isinstance(parsed, dict):
                for _k in ("confidence", "invest_ratio", "target_price", "stop_loss"):
                    if parsed.get(_k) is None:
                        parsed[_k] = 0
            return parsed
        except json.JSONDecodeError as e:
            # 마지막 시도: 정규식으로 핵심 필드만 추출
            import re
            result = {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                      "target_price": 0, "stop_loss": 0,
                      "reasoning": f"JSON 복구: {e}", "long_term_reason": ""}
            m = re.search(r'"signal"\s*:\s*"(\w+)"', text)
            if m:
                result["signal"] = m.group(1)
            m = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
            if m:
                result["confidence"] = float(m.group(1))
            m = re.search(r'"reasoning"\s*:\s*"([^"]{1,500})"', text)
            if m:
                result["reasoning"] = m.group(1)
            return result


class FundamentalAnalyst(_BaseAnalyst):
    """1. 펀더멘털 애널리스트 — 공식 문서 전문."""
    _tier = "balanced"  # 3 CLI 균등 분배: claude/codex/agy 라운드로빈)

    role = "fundamental"
    name_kr = "펀더멘털"

    def _build_prompt(self, stock_code, stock_name, indicators, price_action, extra_context):
        today = datetime.now().strftime("%Y-%m-%d")
        ind_str = json.dumps(indicators, ensure_ascii=False)  # compact(indent 제거) — 입력 토큰 절감

        return f"""당신은 '펀더멘털 애널리스트'입니다.
공식 기관 문서와 검증된 데이터만을 기반으로 투자 판단을 내립니다.

## 당신의 역할
반드시 웹 검색으로 아래 **공식 문서/데이터만** 조사하세요:
1. **중앙은행 보고서**: 한국은행, Fed 금리 결정, 통화정책 방향
2. **실적 발표**: 분기 매출/영업이익/순이익, 가이던스, 컨센서스 대비
3. **공시(DART/SEC)**: 최근 주요 공시, 대주주 변동, 유상증자, 자사주
4. **증권사 리포트**: 목표주가, 투자의견 변경, 컨센서스
5. **정부 정책**: 산업 육성책, 규제, 세제 변화
6. **국제 기구**: IMF, 세계은행, OECD 경기 전망
7. **경제 지표**: GDP, CPI, PMI, 고용지표

루머/찌라시/SNS 정보는 절대 참고하지 마세요. 검증된 팩트만 사용하세요.

## 종목: {stock_name} ({stock_code})
## 날짜: {today}
{f"## 추가 컨텍스트: {extra_context}" if extra_context else ""}

## 기술적 지표 (참고용)
{ind_str}

## 최근 가격 흐름
{price_action}

{ANALYST_RESPONSE_FORMAT}"""


class SentimentAnalyst(_BaseAnalyst):
    """2. 센티멘트 애널리스트 — 찌라시/심리 전문."""
    _tier = "balanced"  # 3 CLI 균등

    role = "sentiment"
    name_kr = "센티멘트"

    def _build_prompt(self, stock_code, stock_name, indicators, price_action, extra_context):
        today = datetime.now().strftime("%Y-%m-%d")
        ind_str = json.dumps(indicators, ensure_ascii=False)  # compact(indent 제거) — 입력 토큰 절감

        return f"""당신은 '센티멘트 애널리스트'입니다.
시장 심리, 루머, 비공식 정보를 종합하여 남들보다 빠르게 시장 방향을 읽습니다.

## 당신의 역할
웹 검색으로 아래 **비공식/심리 정보**를 집중 조사하세요:
1. **투자 찌라시/루머**: 주식 커뮤니티(네이버 종토방, 더쿠, 클리앙, Reddit 등)에서 돌고 있는 이야기
2. **SNS 동향**: Twitter/X, YouTube 주식 채널에서 해당 종목 언급량/분위기
3. **공포/탐욕 지수**: 시장 전반적 심리 (Fear & Greed Index)
4. **테마/키워드 급등**: 네이버 실검, 구글 트렌드에서 관련 키워드 급상승 여부
5. **개인/외국인/기관 수급**: 누가 사고 누가 파는지, 공매도 잔고
6. **내부자 거래 패턴**: 임원 매수/매도 공시
7. **전쟁/지정학 리스크**: 현재 지정학적 긴장이 투자 심리에 미치는 영향

찌라시는 팩트가 아니므로 확신도에 반영하세요. 근거 없는 루머는 확신도를 낮게 설정.
하지만 여러 소스에서 동일한 방향이면 중요한 신호입니다.

## 종목: {stock_name} ({stock_code})
## 날짜: {today}
{f"## 추가 컨텍스트: {extra_context}" if extra_context else ""}

## 기술적 지표 (참고용)
{ind_str}

## 최근 가격 흐름
{price_action}

{ANALYST_RESPONSE_FORMAT}"""


class QuantAnalyst(_BaseAnalyst):
    """3. 퀀트 애널리스트 — 순수 수치 분석."""
    _tier = "balanced"  # 3 CLI 균등

    role = "quant"
    name_kr = "퀀트"

    def _build_prompt(self, stock_code, stock_name, indicators, price_action, extra_context):
        today = datetime.now().strftime("%Y-%m-%d")
        ind_str = json.dumps(indicators, ensure_ascii=False)  # compact(indent 제거) — 입력 토큰 절감

        return f"""당신은 '퀀트 애널리스트'입니다.
뉴스나 루머는 완전히 무시하고, 오직 숫자와 통계만으로 판단합니다.

## 당신의 역할
아래 데이터를 **수치적으로만** 분석하세요:
1. **가격 패턴**: 지지/저항선, 이중바닥, 헤드앤숄더, 추세선 돌파
2. **이동평균**: 정배열/역배열, 골든크로스/데드크로스 임박 여부
3. **RSI**: 과매수/과매도 수준, 다이버전스 발생 여부
4. **MACD**: 신호선 교차 방향, 히스토그램 추세
5. **볼린저밴드**: 밴드 위치, 스퀴즈(수축) 후 확장 신호
6. **거래량**: 가격 대비 거래량 이상치, 세력 매집/분산 패턴
7. **변동성**: ATR 추세, 변동성 확대/축소 국면
8. **통계적 이상**: 표준편차 2σ 이탈, 평균 회귀 신호
9. **52주 고저 대비 위치**: 어디쯤 있는지

뉴스/심리/루머는 절대 고려하지 마세요. 숫자만 보세요.
웹 검색으로 다른 종목의 기술적 차트 비교도 가능합니다.

## 종목: {stock_name} ({stock_code})
## 날짜: {today}
{f"## 추가 컨텍스트: {extra_context}" if extra_context else ""}

## 기술적 지표
{ind_str}

## 최근 10일 가격 흐름
{price_action}

{ANALYST_RESPONSE_FORMAT}"""


class GeneralistAnalyst(_BaseAnalyst):
    """4. 종합 애널리스트 — 기존 올인원 방식.
    _tier = "balanced" # 3 CLI 균등 — 종합 판단도 라운드로빈

    공식 문서, 심리, 수치를 모두 보고 총체적으로 판단.
    전문 분야 없이 모든 정보를 균형 있게 종합.
    """

    role = "generalist"
    name_kr = "종합"

    def _build_prompt(self, stock_code, stock_name, indicators, price_action, extra_context):
        today = datetime.now().strftime("%Y-%m-%d")
        ind_str = json.dumps(indicators, ensure_ascii=False)  # compact(indent 제거) — 입력 토큰 절감

        return f"""당신은 한국/미국 주식 시장 전문 애널리스트이자 퀀트 트레이더입니다.
모든 정보를 종합하여 투자 판단을 내려주세요.

## 중요 원칙
- 수익 = 실현 수익만 인정 (매도 확정된 것만)
- 단기 매매 vs 장기 투자를 반드시 구분
- 장기 투자 추천 시 구체적 사유 필수
- 수익이 높을수록 해당 패턴에 가중치 부여, 실현 수익 극대화 우선

## 종목: {stock_name} ({stock_code})
## 날짜: {today}
{f"## 추가 컨텍스트: {extra_context}" if extra_context else ""}

## 기술적 지표
{ind_str}

## 최근 가격 흐름
{price_action}

## 분석 요청
웹 검색으로 아래를 **모두** 조사하여 종합 판단하세요:
1. 추세/기술적 분석 (이동평균, RSI, MACD, 볼린저)
2. 뉴스/공시/실적 (펀더멘털)
3. 시장 심리/수급 (외국인/기관, 커뮤니티 분위기)
4. 거시 경제 (금리, 환율, 지정학 리스크)
5. 종합 판단 + 목표가/손절가

{ANALYST_RESPONSE_FORMAT}"""


# ══════════════════════════════════════
# 심판 (Judge) — 4인 분석 합산 + 성과 경쟁
# ══════════════════════════════════════

class ClaudeAnalyst:
    """4인 경쟁 애널리스트 + 심판.

    각 애널리스트의 과거 정확도를 추적하여,
    맞춘 횟수가 많은 애널리스트에게 더 높은 가중치를 부여.

    1. 펀더멘털 — 공식 문서/실적/공시만
    2. 센티멘트 — 찌라시/루머/심리/수급
    3. 퀀트     — 순수 수치/패턴/통계
    4. 종합     — 기존 올인원 (모든 정보 균형 종합)
    """

    ROLES = ["fundamental", "sentiment", "quant", "generalist"]

    def __init__(self, api_key: str = "", model: str = "claude-sonnet-5",
                 prefer_cli: bool = True, provider: str = "auto"):
        from zusik.clients.claude_client import ClaudeClient
        client = ClaudeClient(api_key=api_key, prefer_cli=prefer_cli, provider=provider)
        self._client = client
        self.model = model

        self.analysts = {
            "fundamental": FundamentalAnalyst(client, model),
            "sentiment": SentimentAnalyst(client, model),
            "quant": QuantAnalyst(client, model),
            "generalist": GeneralistAnalyst(client, model),
        }

        # 퀀트는 웹 검색 안 함 (숫자만)
        self.analysts["quant"]._use_web_search = False

        # 성과 추적
        self._perf: dict = _load_perf()
        for role in self.ROLES:
            if role not in self._perf:
                self._perf[role] = {"correct": 0, "wrong": 0, "total": 0, "weight": 1.0}

        # 기억 시스템
        from zusik.analysis.claude_memory import ClaudeMemory
        self.memory = ClaudeMemory()

    # ── 4인 분석 실행 ──

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        df: pd.DataFrame,
        use_web_search: bool = True,
        portfolio_info: str = "",
        long_term_info: str = "",
    ) -> dict:
        """4인 독립 분석 → 가중 합산 → 최종 판단."""

        logger.info("══ 4인 경쟁 분석: %s(%s) ══", stock_name, stock_code)

        indicators = calc_indicators(df)
        price_action = summarize_price_action(df)

        # 기억 주입: 과거 매매 결과, 실수 교훈, 종목 관찰
        memory_text = self.memory.build_memory_prompt(stock_code)
        extra_parts = [portfolio_info, long_term_info]
        if memory_text:
            extra_parts.append(memory_text)
        extra = "\n".join(p for p in extra_parts if p)

        # 선별된 애널리스트만 병렬 호출
        selected = getattr(self, "_selected_roles", None) or list(self.analysts.keys())
        self._selected_roles = None

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _analyze_one(role):
            analyst = self.analysts.get(role)
            if not analyst:
                return role, None
            logger.info("  [%s] 분석 중...", analyst.name_kr)
            result = analyst.analyze(stock_code, stock_name, df, indicators, price_action, extra)
            sig = result.get("signal", "hold")
            conf = result.get("confidence", 0)
            logger.info("  [%s] → %s (확신도 %.0f%%): %s",
                        analyst.name_kr, sig.upper(), conf * 100,
                        result.get("reasoning", "")[:80])
            return role, result

        results = {}
        from concurrent.futures import TimeoutError as _FutTimeout
        executor = ThreadPoolExecutor(max_workers=len(selected))
        futures = {executor.submit(_analyze_one, role): role for role in selected}
        try:
            # 개별 _run_claude(웹검색)는 max 300초.
            # 두 명이 거의 동시에 시작해도 GIL/네트워크 지연으로 직렬화되는 부분 있음 →
            # 글로벌은 600초로 두텁게 잡아 타임아웃 placeholder 양산 방지.
            for future in as_completed(futures, timeout=600):
                try:
                    role, result = future.result()
                    if result:
                        results[role] = result
                except Exception as e:
                    role = futures[future]
                    logger.warning("  [%s] 병렬 분석 실패: %s", role, e)
                    results[role] = {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                                     "target_price": 0, "stop_loss": 0, "reasoning": f"분석 실패: {e}",
                                     "long_term_reason": ""}
        except _FutTimeout:
            logger.warning("Claude 병렬 분석 타임아웃 — 미완료 애널리스트는 hold로 처리")
            for fut, role in futures.items():
                if not fut.done():
                    fut.cancel()
                    if role not in results:
                        results[role] = {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                                         "target_price": 0, "stop_loss": 0,
                                         "reasoning": "타임아웃 — Claude CLI 응답 없음",
                                         "long_term_reason": ""}
        finally:
            executor.shutdown(wait=False)

        # 미호출 애널리스트는 hold로 채움
        for role in self.ROLES:
            if role not in results:
                results[role] = {"signal": "hold", "confidence": 0, "invest_ratio": 0,
                                 "target_price": 0, "stop_loss": 0, "reasoning": "미호출",
                                 "long_term_reason": ""}

        # 가중 합산
        final = self._judge(results, indicators)

        logger.info("══ 최종 판단: %s | 확신도 %.0f%% | 투자비율 %.0f%% ══",
                     final["signal"].upper(), final["confidence"] * 100,
                     final["invest_ratio"] * 100)

        return final

    def _judge(self, results: dict[str, dict], indicators: dict) -> dict:
        """호출된 애널리스트만으로 가중 합산 (미호출/타임아웃 제외)."""

        # 호출도 못 된 + 타임아웃·실패도 모두 제외.
        # placeholder reason들이 active_roles에 섞이면 의미없는 hold 표가 가중치를
        # 차지해 BUY=0% 같은 깨진 합의가 나옴.
        SKIP_REASONS = ("미호출", "타임아웃 — Claude CLI 응답 없음")
        active_roles = [
            r for r in self.ROLES
            if results.get(r, {}).get("reasoning") not in SKIP_REASONS
            and not str(results.get(r, {}).get("reasoning", "")).startswith("분석 실패:")
        ]
        if not active_roles:
            # 활성 애널리스트가 0이면 명시적 hold — BUY로 잘못 떨어지는 버그 차단
            return {
                "signal": "hold",
                "confidence": 0.0,
                "invest_ratio": 0.0,
                "target_price": 0,
                "stop_loss": 0,
                "reasoning": "활성 애널리스트 0명 (모두 타임아웃/미호출/실패)",
                "long_term_reason": "",
                "news_summary": "",
                "market_overview": "",
                "indicators": indicators,
                "analyst_details": {
                    role: {
                        "signal": results.get(role, {}).get("signal", "hold"),
                        "confidence": results.get(role, {}).get("confidence", 0),
                        "invest_ratio": results.get(role, {}).get("invest_ratio", 0),
                        "weight": 0,
                        "reasoning": results.get(role, {}).get("reasoning", ""),
                    }
                    for role in self.ROLES
                },
            }

        weights = {role: self._perf[role]["weight"] for role in active_roles}
        total_w = sum(weights.values()) or 1
        norm = {role: w / total_w for role, w in weights.items()}

        # 신호 집계 (호출된 애널리스트만)
        signal_scores = {"buy": 0, "long_term_buy": 0, "sell": 0, "hold": 0}
        weighted_confidence = 0
        weighted_invest_ratio = 0
        target_prices = []
        stop_losses = []
        all_reasoning = []
        long_term_reasons = []

        for role in active_roles:
            r = results[role]
            w = norm[role]
            sig = r.get("signal", "hold")
            #: 키가 None으로 존재하면 .get(...,0)이 None 반환 → None>0/곱셈 TypeError
            # (Dell 분석이 stop_loss=null 반환해 _judge 크래시). `or 0`으로 None→0 정규화.
            conf = r.get("confidence") or 0

            if sig in signal_scores:
                signal_scores[sig] += w * conf
            weighted_confidence += w * conf
            weighted_invest_ratio += w * (r.get("invest_ratio") or 0)

            if (r.get("target_price") or 0) > 0:
                target_prices.append(r["target_price"])
            if (r.get("stop_loss") or 0) > 0:
                stop_losses.append(r["stop_loss"])

            name_kr = self.analysts[role].name_kr
            pct = norm[role] * 100
            # '가중' 명시 — 이전 "[펀더멘털 47%]" 표기가 확신도로 오독되던 문제
            all_reasoning.append(f"[{name_kr} 가중 {pct:.0f}%] {r.get('reasoning', '')}")
            if r.get("long_term_reason"):
                long_term_reasons.append(f"[{name_kr}] {r['long_term_reason']}")

        # 최다 득표 신호 — 모든 점수 0이면 안전하게 hold.
        # 기존 `max`는 dict 순서로 첫 키("buy")를 반환해 "BUY 확신도 0%" 같은
        # 거짓 양성 메시지를 만들어냈음.
        if max(signal_scores.values()) <= 0:
            best_signal = "hold"
        else:
            best_signal = max(signal_scores, key=signal_scores.get)

        # 장기매수인데 사유 없으면 일반 매수로 변환
        if best_signal == "long_term_buy" and not long_term_reasons:
            best_signal = "buy"

        # 다수결 안전장치 —
        # 표가 4분할되면 1표 차로 LONG_TERM_BUY가 채택되는 거짓 양성이 발생했음.
        # 예: 퀀트 SELL 68% + 종합 BUY 70% + 센티 HOLD 60% + 펀더 LONG_TERM_BUY 75%
        # → 채택 LONG_TERM_BUY (1/4 표). 직관에 안 맞음.
        # 룰: 매도 표 ≥ 매수 합(buy + long_term_buy)이면 매수계 결정 차단.
        signals_pre = [results[r].get("signal", "hold") for r in active_roles]
        buy_pre = sum(1 for s in signals_pre if s in ("buy", "long_term_buy"))
        sell_pre = sum(1 for s in signals_pre if s == "sell")
        n_pre = len(signals_pre)

        if best_signal in ("buy", "long_term_buy") and sell_pre >= buy_pre and n_pre >= 3:
            # 매도가 매수계와 같거나 많으면 매수 차단
            if sell_pre > buy_pre:
                best_signal = "sell"
                all_reasoning.append(f"[다수결 가드: 매도 {sell_pre}/{n_pre} → 매수 차단]")
            else:
                best_signal = "hold"
                all_reasoning.append(f"[다수결 가드: 매도/매수 동수 ({sell_pre}:{buy_pre}) → hold]")
        elif best_signal == "sell" and buy_pre > sell_pre and n_pre >= 3:
            # 매수계가 매도보다 많은데 sell이 점수만 높을 때 hold로 안전화
            best_signal = "hold"
            all_reasoning.append(f"[다수결 가드: 매수 {buy_pre} > 매도 {sell_pre} → hold]")

        # 합의 가산점 / 분열 감점 — 다수결 비율 기반
        signals = [results[r].get("signal", "hold") for r in active_roles]
        buy_signals = {"buy", "long_term_buy"}
        n = len(signals)
        buy_count = sum(1 for s in signals if s in buy_signals)
        sell_count = sum(1 for s in signals if s == "sell")

        # 만장일치 (n/n): n≥3이면 +20%, n=2는 +10% — quick 모드(2명)에서 2/2 만장일치가
        # 상시 +20%p를 먹어 확신도를 부풀리던 문제(실측: 7일간 8회, 0.74→0.94 등).
        # 표본 2개의 합의는 절반만 신뢰한다.
        if n >= 3 and (buy_count == n or sell_count == n):
            weighted_confidence = min(1.0, weighted_confidence + 0.2)
            all_reasoning.append(f"[만장일치 +20% ({max(buy_count, sell_count)}/{n})]")
        elif n == 2 and (buy_count == n or sell_count == n):
            weighted_confidence = min(1.0, weighted_confidence + 0.1)
            all_reasoning.append(f"[만장일치(2인) +10% ({max(buy_count, sell_count)}/{n})]")
        # 압도적 다수결 (n-1, 반대 0 — 나머지는 hold): +15%
        elif n >= 3 and buy_count >= n - 1 and sell_count == 0:
            weighted_confidence = min(1.0, weighted_confidence + 0.15)
            all_reasoning.append(f"[다수결 매수 +15% ({buy_count}/{n}, 반대 없음)]")
        elif n >= 3 and sell_count >= n - 1 and buy_count == 0:
            weighted_confidence = min(1.0, weighted_confidence + 0.15)
            all_reasoning.append(f"[다수결 매도 +15% ({sell_count}/{n}, 반대 없음)]")
        # 우세 다수결 (3:1처럼 한쪽 우세): +10%
        elif n >= 4 and buy_count >= 3 and sell_count == 1:
            weighted_confidence = min(1.0, weighted_confidence + 0.1)
            all_reasoning.append(f"[우세 매수 +10% ({buy_count}/{n})]")
        elif n >= 4 and sell_count >= 3 and buy_count == 1:
            weighted_confidence = min(1.0, weighted_confidence + 0.1)
            all_reasoning.append(f"[우세 매도 +10% ({sell_count}/{n})]")
        # 진짜 분열 (2:2 이상): -40%
        elif buy_count >= 2 and sell_count >= 2:
            weighted_confidence *= 0.6
            all_reasoning.append(f"[의견 분열 -40% (매수 {buy_count}/매도 {sell_count})]")
        # 약한 충돌 (1:1, 1:2 등 비등): 감점 없음 — 다수 의견을 그대로 신뢰

        return {
            "signal": best_signal,
            "confidence": round(min(1.0, weighted_confidence), 2),
            "invest_ratio": round(min(1.0, weighted_invest_ratio), 2),
            "target_price": int(np.mean(target_prices)) if target_prices else 0,
            "stop_loss": int(np.mean(stop_losses)) if stop_losses else 0,
            "reasoning": "\n".join(all_reasoning),
            "long_term_reason": "\n".join(long_term_reasons) if long_term_reasons else "",
            "news_summary": "",
            "market_overview": "",
            "indicators": indicators,
            # 4명 ROLES 전체 포함 (claude_quick 2인 모드여도 비활성은 reason만 표기).
            # 이전엔 active_roles만 + reasoning[:100] 자름 → /분석 결과에서 펀더·센티
            # 누락 + 퀀트 reasoning 100자만 보이는 문제.
            "analyst_details": {
                role: {
                    "signal": results.get(role, {}).get("signal", "hold"),
                    "confidence": results.get(role, {}).get("confidence", 0),
                    "weight": round(norm.get(role, 0), 2) if role in active_roles else 0,
                    "reasoning": results.get(role, {}).get("reasoning", ""),  # 전체 표시
                }
                for role in self.ROLES
            },
            "weights": {role: round(norm.get(role, 0), 2) for role in active_roles},
            "alternative_picks": self._collect_alternatives(results, active_roles),
        }

    @staticmethod
    def _collect_alternatives(results: dict, active_roles: list) -> list[str]:
        """에이전트들이 추천한 대안 종목 수집."""
        picks = []
        for role in active_roles:
            alt = results.get(role, {}).get("alternative_pick", "")
            if alt and alt.strip():
                picks.append(alt.strip())
        # 중복 제거
        return list(dict.fromkeys(picks))

    # ── 성과 추적 (매도 후 호출) ──

    def record_result(self, analyst_predictions: dict, actual_pnl: float,
                      stock_code: str = "", stock_name: str = ""):
        """매도 체결 후, 각 애널리스트가 맞았는지 기록 + 기억 갱신."""
        is_profit = actual_pnl > 0

        # 기억에 결과 기록
        self.memory.record_outcome(stock_code, actual_pnl)
        if not is_profit and actual_pnl < -3:
            # 큰 손실이면 어떤 애널리스트가 틀렸는지 기억
            wrong_analysts = []
            for role in self.ROLES:
                pred = analyst_predictions.get(role, {})
                if pred.get("signal") in ("buy", "long_term_buy"):
                    wrong_analysts.append(self.analysts[role].name_kr)
            if wrong_analysts:
                self.memory.add_mistake(
                    stock_code,
                    f"{stock_name} {actual_pnl:+.1f}% 손실. "
                    f"매수 추천한 애널리스트: {', '.join(wrong_analysts)}. "
                    f"근거를 재검토할 것."
                )

        for role in self.ROLES:
            pred = analyst_predictions.get(role, {})
            sig = pred.get("signal", "hold")

            # 매수 신호 → 수익이면 correct
            was_buy = sig in ("buy", "long_term_buy")
            was_sell = sig == "sell"

            correct = (was_buy and is_profit) or (was_sell and not is_profit)

            p = self._perf[role]
            p["total"] += 1
            if correct:
                p["correct"] += 1
            else:
                p["wrong"] += 1

            # 가중치 재계산: 정확도 기반 (최소 0.5, 최대 2.0)
            # 표본 10건 이상부터 본격 반영 + 베이지안 prior로 부드럽게
            # (0.5 prior accuracy, 5건 가상 표본 → 작은 표본일 때 weight 1.0 근처 유지)
            if p["total"] >= 10:
                prior_acc = 0.5
                prior_n = 5
                accuracy = (p["correct"] + prior_acc * prior_n) / (p["total"] + prior_n)
                p["weight"] = max(0.5, min(2.0, 0.5 + accuracy * 1.5))

        _save_perf(self._perf)

        logger.info("애널리스트 성과 갱신 (실현 %+.2f%%)", actual_pnl)
        for role in self.ROLES:
            p = self._perf[role]
            acc = (p["correct"] / p["total"] * 100) if p["total"] > 0 else 0
            logger.info("  [%s] %d전 %d승 (%.0f%%) → 가중치 %.2f",
                        self.analysts[role].name_kr, p["total"], p["correct"], acc, p["weight"])

    def get_analyst_standings(self) -> dict:
        """현재 애널리스트별 성적표."""
        standings = {}
        for role in self.ROLES:
            p = self._perf[role]
            standings[self.analysts[role].name_kr] = {
                "role": role,
                "total": p["total"],
                "correct": p["correct"],
                "accuracy": round(p["correct"] / p["total"] * 100, 1) if p["total"] > 0 else 0,
                "weight": round(p["weight"], 2),
            }
        return standings

    # ── 시장 동향 조사 (장 시작 전 알림용) ──

    def research_stock(self, stock_code: str, stock_name: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        prompt = f"오늘 날짜: {today}\n{stock_name}({stock_code}) 최근 뉴스/공시/위험요소를 조사해줘. 한국어로 간결하게."
        try:
            return self._client.message(prompt, use_web_search=True)
        except Exception:
            return ""

    def confirm_critical_danger(self, stock_code: str, stock_name: str,
                                 suspected_keywords: list) -> dict:
        """키워드가 의심되는 뉴스에 대해 LLM에게 직접 yes/no 확인.

        substring 매칭의 false positive (부정·비교·간접 문맥)를 거르기 위함.
        예: "상장폐지 우려 없음" → keyword '상장폐지' 매칭되지만 실제 위험은 없음.

        Returns:
            {"confirmed": bool, "reason": str (1줄 요약)}
        """
        if not suspected_keywords:
            return {"confirmed": False, "reason": "no keywords"}
        today = datetime.now().strftime("%Y-%m-%d")
        kw = ", ".join(suspected_keywords)
        prompt = (
            f"오늘 날짜: {today}\n"
            f"{stock_name}({stock_code}) 종목에 대해 다음 위험이 **현재 시점에 실제로 진행 중**인지 확인해줘:\n"
            f"의심 키워드: {kw}\n\n"
            f"질문: {stock_name}이(가) 지금 이 키워드들 중 하나라도 직접 해당되어 "
            f"**상장폐지·거래정지·관리종목 지정·파산 절차가 임박했거나 진행 중**인가?\n\n"
            f"부정 문맥(예: '상장폐지 우려 없음'), 비교 문맥(예: '타사 사례'), "
            f"과거 사건이 이미 해소된 경우는 모두 NO로 답해.\n\n"
            f"형식 (정확히 따를 것):\n"
            f"답: YES 또는 NO\n"
            f"이유: (한 줄, 30자 이내)\n"
        )
        try:
            resp = self._client.message(prompt, use_web_search=True)
        except Exception:
            return {"confirmed": False, "reason": "LLM 호출 실패 — 보수적 NO"}
        text = (resp or "").strip()
        # YES/NO 파싱 (대소문자/공백 무관, 첫 번째 등장 우선)
        upper = text.upper()
        yes_idx = upper.find("YES")
        no_idx = upper.find("NO")
        confirmed = False
        if yes_idx >= 0 and (no_idx < 0 or yes_idx < no_idx):
            confirmed = True
        # 이유 추출 (간단 — '이유:' 뒤 첫 줄)
        reason = ""
        if "이유" in text:
            try:
                reason = text.split("이유")[1].split("\n")[0].lstrip(":： ").strip()[:80]
            except Exception:
                reason = ""
        if not reason:
            reason = text[:80]
        return {"confirmed": confirmed, "reason": reason}
