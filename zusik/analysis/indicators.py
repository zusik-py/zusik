from __future__ import annotations
"""순수 수치 지표 계산 모듈 — 벡터화된 NumPy/Pandas 구현.

설계 원칙 (차후 VCK5000/FPGA 확장 대비):
  - 모든 함수는 순수 함수 (입력 DataFrame → 스칼라/Series, 부수효과 없음)
  - 입력: OHLCV DataFrame 또는 numpy ndarray
  - 배치 버전(batch_*): 여러 종목의 ndarray를 한 번에 받아 병렬 계산
    → 차후 Vitis AI/FPGA로 오프로드 시 이 함수만 교체하면 됨

함수 분류:
  - breakout_signal: N일 고점 돌파 여부
  - volume_surge: 거래량 배수
  - atr: Average True Range
  - relative_strength: 기준 지수 대비 상대강도
  - momentum_score: 복합 모멘텀 점수
"""

from typing import Iterable

import numpy as np
import pandas as pd


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder 호환 RSI 시계열.

    공용 함수가 없는데 매수 가드가 이 이름을 import한 뒤 예외를 삼키고 있어 실제로는
    RSI 추격 차단이 한 번도 작동하지 않았다. DataFrame 입력을 표준으로 통일한다.
    """
    if df is None or "close" not in df.columns:
        return pd.Series(dtype=float)
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # 상승만 있는 구간(loss=0)은 RSI 100, 하락만 있는 구간(gain=0)은 0.
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    return rsi


def breakout_signal(df: pd.DataFrame, lookback: int = 20) -> dict:
    """N일 고점 돌파 시그널.

    Returns:
        {
          "is_breakout": bool,
          "prior_high": float,
          "distance_pct": float (현재가와 고점 대비 %)
        }
    """
    if df is None or len(df) < lookback + 1:
        return {"is_breakout": False, "prior_high": 0.0, "distance_pct": 0.0}
    prior_high = float(df["high"].iloc[-lookback - 1:-1].max())
    curr_close = float(df["close"].iloc[-1])
    distance = (curr_close - prior_high) / prior_high if prior_high > 0 else 0.0
    return {
        "is_breakout": curr_close > prior_high,
        "prior_high": prior_high,
        "distance_pct": distance,
    }


def volume_surge(df: pd.DataFrame, window: int = 20, threshold: float = 2.0) -> dict:
    """거래량 폭증 감지.

    Returns:
        {"is_surge": bool, "ratio": 현재/평균 거래량 배수}
    """
    if df is None or len(df) < window + 1 or "volume" not in df.columns:
        return {"is_surge": False, "ratio": 0.0}
    avg_vol = float(df["volume"].iloc[-window - 1:-1].mean())
    curr_vol = float(df["volume"].iloc[-1])
    ratio = curr_vol / avg_vol if avg_vol > 0 else 0.0
    return {"is_surge": ratio >= threshold, "ratio": ratio}


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range."""
    if df is None or len(df) < period + 1:
        return 0.0
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def momentum_score(df: pd.DataFrame, periods: Iterable[int] = (5, 20, 60)) -> float:
    """다중 기간 모멘텀 가중 평균 ( (-1 ~ +1) 정규화).

    각 기간의 수익률을 tanh로 squash한 후 평균. +1에 가까울수록 강한 상승 모멘텀.
    데이터가 짧으면 사용 가능한 기간만 사용.
    """
    if df is None or len(df) < min(periods) + 1:
        return 0.0
    close = df["close"].values
    scores = []
    for p in periods:
        if len(close) <= p:
            continue
        ret = (close[-1] - close[-p - 1]) / close[-p - 1] if close[-p - 1] > 0 else 0
        scores.append(float(np.tanh(ret * 5)))  # tanh squash with gain 5
    return float(np.mean(scores)) if scores else 0.0


def hold_score(df: pd.DataFrame) -> dict:
    """상승 지속 가능성 로컬 점수 (0.0~1.0).

    매도 신호가 나왔을 때 "추가 상승 여지"를 평가해 연기 여부를 판단하는 용도.
    Claude 호출 없이 가격/거래량 시계열만으로 계산.

    구성 (각 기여도):
      - 복합 모멘텀 momentum_score (최대 0.35)
      - MA 정배열 (close > MA5 > MA20, 0.25)
      - 거래량 증가 (최근 > 10봉 평균 × 1.2, 최대 0.15)
      - 고점 근접도 (최근 20봉 고점 대비 -3% 이내 0.25, -7% 이내 0.10)

    Returns:
        {"score": 0.0~1.0, "momentum": -1..+1, "ma_bull": bool,
         "vol_ratio": float, "pullback": float}
    """
    if df is None or len(df) < 20 or "close" not in df.columns:
        return {"score": 0.5, "momentum": 0.0, "ma_bull": False,
                "vol_ratio": 0.0, "pullback": 0.0}
    closes = df["close"]
    curr = float(closes.iloc[-1])

    m = momentum_score(df)

    ma5 = float(closes.rolling(5).mean().iloc[-1])
    ma20 = float(closes.rolling(20).mean().iloc[-1])
    ma_bull = curr > ma5 > ma20

    vs = volume_surge(df, window=10, threshold=1.2)
    vol_ratio = float(vs.get("ratio", 0.0))

    high20 = float(closes.tail(20).max())
    pullback = (high20 - curr) / high20 if high20 > 0 else 0.0

    score = 0.0
    if m > 0.2:
        score += 0.35
    elif m > 0.0:
        score += 0.15
    if ma_bull:
        score += 0.25
    if vol_ratio >= 1.2:
        score += 0.15
    elif vol_ratio >= 1.0:
        score += 0.05
    if pullback < 0.03:
        score += 0.25
    elif pullback < 0.07:
        score += 0.10

    return {
        "score": max(0.0, min(1.0, score)),
        "momentum": m,
        "ma_bull": ma_bull,
        "vol_ratio": vol_ratio,
        "pullback": pullback,
    }


def slow_bleed(df: pd.DataFrame, lookback: int = 5,
                min_down_ratio: float = 0.6,
                min_cum_drop: float = 0.03) -> dict:
    """느린 출혈 감지 — 급락은 아니지만 꾸준히 밀리는 패턴.

    조건:
      - 최근 N봉 중 음봉 비율 ≥ min_down_ratio (기본 60%)
      - N봉 누적 하락률 ≤ -min_cum_drop (기본 -3%)
      - 저점이 지속적으로 낮아짐 (lower lows)

    Returns:
        {
          "is_bleeding": bool,
          "down_ratio": 음봉 비율,
          "cum_drop": 누적 하락률,
          "lower_lows": 저점 갱신 횟수,
        }
    """
    if df is None or len(df) < lookback + 1:
        return {"is_bleeding": False, "down_ratio": 0.0,
                "cum_drop": 0.0, "lower_lows": 0}
    window = df.iloc[-lookback:]
    down_bars = int((window["close"] < window["open"]).sum())
    down_ratio = down_bars / lookback
    cum_drop = float((window["close"].iloc[-1] - df["close"].iloc[-lookback - 1])
                     / df["close"].iloc[-lookback - 1]) if df["close"].iloc[-lookback - 1] > 0 else 0.0
    lows = window["low"].values
    lower_lows = int(sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1]))
    is_bleeding = (
        down_ratio >= min_down_ratio
        and cum_drop <= -min_cum_drop
        and lower_lows >= lookback // 2
    )
    return {
        "is_bleeding": is_bleeding,
        "down_ratio": down_ratio,
        "cum_drop": cum_drop,
        "lower_lows": lower_lows,
    }


# ══════════════════════════════════════════════════
# 배치 버전 (차후 FPGA 오프로드용)
# ══════════════════════════════════════════════════

