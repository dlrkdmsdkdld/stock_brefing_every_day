"""종가 시계열에서 RSI와 볼린저밴드를 계산한다.

외부 의존성 없이 순수 함수로 두어 값 검증이 쉽도록 했다.
입력은 오래된 것부터 정렬된 종가 리스트다.
"""
import math

RSI_PERIOD = 14
BB_PERIOD = 20
BB_SIGMA = 2.0


def rsi(closes, period=RSI_PERIOD):
    """와일더 방식 RSI. 표준 계산법이며 대부분의 차트 프로그램과 같다.

    첫 period개 변화량의 단순평균으로 시작해 이후를 지수적으로 완만하게 갱신한다.
    """
    if len(closes) < period + 1:
        raise ValueError(f"RSI{period} 계산에 종가 {period + 1}개 필요, {len(closes)}개뿐")
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def bollinger(closes, period=BB_PERIOD, sigma=BB_SIGMA):
    """볼린저밴드. 이동평균 period일, 표준편차 sigma배.

    표준편차는 모집단 기준(ddof=0)으로, TradingView 등 일반적인 차트와 같은 방식이다.
    """
    if len(closes) < period:
        raise ValueError(f"볼린저밴드({period}) 계산에 종가 {period}개 필요, {len(closes)}개뿐")
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((value - middle) ** 2 for value in window) / period
    deviation = math.sqrt(variance)
    upper = middle + sigma * deviation
    lower = middle - sigma * deviation
    close = closes[-1]
    if upper == lower:
        position, percent_b = "밴드 내", 0.5
    else:
        percent_b = (close - lower) / (upper - lower)
        position = ("상단 이탈" if close > upper
                    else "하단 이탈" if close < lower else "밴드 내")
    return dict(upper=upper, middle=middle, lower=lower,
                position=position, percent_b=percent_b,
                bandwidth=(upper - lower) / middle if middle else None)


def zone(value):
    """RSI 구간. 통상 70 이상 과매수, 30 이하 과매도로 본다."""
    if value >= 70:
        return "과매수"
    if value <= 30:
        return "과매도"
    return "중립"


def compute(closes):
    """가능한 지표만 계산해 돌려준다. 데이터가 모자라면 그 사실을 남긴다."""
    result = {}
    try:
        value = rsi(closes)
        result["rsi"] = round(value, 1)
        result["rsi_zone"] = zone(value)
    except ValueError as exc:
        result["rsi_error"] = str(exc)
    try:
        band = bollinger(closes)
        result.update(
            bb_upper=round(band["upper"], 4), bb_middle=round(band["middle"], 4),
            bb_lower=round(band["lower"], 4), bb_position=band["position"],
            bb_percent_b=round(band["percent_b"], 3))
    except ValueError as exc:
        result["bb_error"] = str(exc)
    result["samples"] = len(closes)
    return result
