"""Pure-Python technical indicators (no numpy dependency)."""
from __future__ import annotations

import math


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period or period <= 0:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    # align: out[i] corresponds to values[period-1+i]
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI of the last value."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(values) < slow + signal:
        return None
    ef = ema(values, fast)
    es = ema(values, slow)
    # align fast EMA to slow EMA
    ef = ef[len(ef) - len(es):]
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    if len(sig) < 2:
        return None
    hist_now = line[-1] - sig[-1]
    hist_prev = line[-2] - sig[-2]
    return {"macd": line[-1], "signal": sig[-1], "hist": hist_now, "hist_prev": hist_prev}


def atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period
    return a


def swing_levels(candles: list[dict], lookback: int = 48, wing: int = 2) -> dict:
    """Nearest swing support below and resistance above the last close."""
    recent = candles[-lookback:]
    last = candles[-1]["close"]
    highs, lows = [], []
    for i in range(wing, len(recent) - wing):
        h = recent[i]["high"]
        l = recent[i]["low"]
        if all(h >= recent[j]["high"] for j in range(i - wing, i + wing + 1)):
            highs.append(h)
        if all(l <= recent[j]["low"] for j in range(i - wing, i + wing + 1)):
            lows.append(l)
    res = min((h for h in highs if h > last), default=None)
    sup = max((l for l in lows if l < last), default=None)
    return {
        "support": sup,
        "resistance": res,
        "range_high": max(c["high"] for c in recent),
        "range_low": min(c["low"] for c in recent),
    }


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
