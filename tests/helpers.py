"""Test helpers: fake clock, synthetic candles, and a fake read-only market client."""
from __future__ import annotations

import math
import os
import tempfile

from app.config import Settings

H = 3_600_000
M15 = 900_000
M1 = 60_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % H)  # aligned hour, Oct 2025


class Clock:
    def __init__(self, t=T0 + 300 * H):
        self.t = t

    def __call__(self):
        return self.t


def settings(**kw) -> Settings:
    s = Settings()
    s.data_dir = tempfile.mkdtemp(prefix="paper-test-")
    s.request_pause_seconds = 0
    s.favorites = ["BTCUSDT"]
    s.universe_top_n = 3
    s.ai_mode = "off"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def trend_candles(end_ms: int, interval: int, n: int = 200, start: float = 100.0, drift: float = 0.0008,
                  wiggle: float = 0.004, accel_last: int = 6) -> list[dict]:
    """Up-trending series with pullbacks (keeps RSI moderate) and a slight acceleration at the end."""
    out, price = [], start
    first = end_ms - n * interval
    for i in range(n):
        step = drift * (1.6 if i >= n - accel_last else 1.0)
        osc = wiggle * math.sin(i * 1.3)
        o = price
        c = price * (1 + step + osc - wiggle * math.sin((i - 1) * 1.3))
        hi, lo = max(o, c) * 1.0015, min(o, c) * 0.9985
        out.append({"time": first + i * interval, "open": o, "high": hi, "low": lo, "close": c, "quote_volume": 1e6 * (1.3 if i >= n - 4 else 1.0)})
        price = c
    return out


def flat_candles(start_ms: int, interval: int, n: int, price: float) -> list[dict]:
    return [{"time": start_ms + i * interval, "open": price, "high": price * 1.0005, "low": price * 0.9995,
             "close": price, "quote_volume": 1e6} for i in range(n)]


class FakeClient:
    """Mimics BitunixPublicClient. Serves synthetic data; can be told to fail."""

    def __init__(self, clock: Clock, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"), fail_tickers=False):
        self.clock = clock
        self.symbols = symbols
        self.fail_tickers = fail_tickers
        self.request_count = 0
        self.minute_candles: dict[str, list[dict]] = {}

    def tickers(self):
        self.request_count += 1
        if self.fail_tickers:
            from app.bitunix import MarketDataError
            raise MarketDataError("simulated outage")
        rows = []
        for s in self.symbols:
            c = self.klines(s, "15m", 200)[-1]
            rows.append({"symbol": s, "lastPrice": str(c["close"]), "quoteVol": "50000000"})
        return rows

    def klines(self, symbol, interval="15m", limit=200, start_ms=None, end_ms=None):
        self.request_count += 1
        now = self.clock()
        if interval == "1m":
            rows = [c for c in self.minute_candles.get(symbol, []) if c["time"] <= now]
            if start_ms is not None:
                rows = [c for c in rows if c["time"] >= start_ms]
            return rows[:limit]
        step = {"15m": M15, "1h": H}[interval]
        end = (now // step) * step  # end of the last fully closed candle
        base = {"BTCUSDT": 100.0, "ETHUSDT": 50.0}.get(symbol, 10.0)
        if symbol == "SOLUSDT":  # flat market -> NO TRADE
            rows = flat_candles(end - limit * step, step, limit, base)
        else:
            rows = trend_candles(end, step, n=limit, start=base)
        # like the real API, also return the currently forming candle (the scanner must drop it)
        last = rows[-1]
        forming = dict(last, time=last["time"] + step, open=last["close"], high=last["close"] * 1.05, low=last["close"] * 0.95)
        return (rows + [forming])[-limit:]

    def funding_rate(self, symbol):
        self.request_count += 1
        return 0.0001
