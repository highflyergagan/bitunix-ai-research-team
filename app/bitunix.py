"""Read-only client for Bitunix PUBLIC futures market data.

There is no authentication, no signing, and no order, withdrawal or transfer code here.
Only GET requests to an explicit allow-list of public market endpoints are possible.
"""
from __future__ import annotations

import time
from typing import Any

import requests

BASE_URL = "https://fapi.bitunix.com"

ALLOWED_PATHS = frozenset(
    {
        "/api/v1/futures/market/tickers",
        "/api/v1/futures/market/kline",
        "/api/v1/futures/market/funding_rate",
    }
)

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


class MarketDataError(RuntimeError):
    pass


class BitunixPublicClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 12.0, retries: int = 2, pause: float = 0.15):
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "bitunix-paper-research/1.0 (read-only)"
        self.request_count = 0

    def _get(self, path: str, params: dict | None = None) -> Any:
        if path not in ALLOWED_PATHS:
            raise PermissionError(f"Path {path!r} is not an allowed public market-data endpoint")
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                if self.pause:
                    time.sleep(self.pause)
                self.request_count += 1
                r = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    raise MarketDataError("rate limited (429)")
                r.raise_for_status()
                j = r.json()
                if j.get("code") not in (0, "0"):
                    raise MarketDataError(f"Bitunix error {j.get('code')}: {j.get('msg')}")
                return j.get("data")
            except (requests.RequestException, ValueError, MarketDataError) as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 5))
        raise MarketDataError(f"{path} failed: {last_exc}")

    def tickers(self) -> list[dict]:
        return self._get("/api/v1/futures/market/tickers") or []

    def klines(self, symbol: str, interval: str = "15m", limit: int = 200,
               start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": min(limit, 200)}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        rows = self._get("/api/v1/futures/market/kline", params) or []
        out = []
        for x in rows:
            try:
                out.append({
                    "time": int(x["time"]),
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"]),
                    "quote_volume": float(x.get("quoteVol") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c["time"])
        # de-duplicate by open time
        dedup: dict[int, dict] = {c["time"]: c for c in out}
        return [dedup[k] for k in sorted(dedup)]

    def funding_rate(self, symbol: str) -> float | None:
        """Current funding rate as a fraction (0.0001 = 0.01%). None if unavailable."""
        try:
            d = self._get("/api/v1/futures/market/funding_rate", {"symbol": symbol})
        except MarketDataError:
            return None
        if isinstance(d, list):
            d = d[0] if d else None
        if not isinstance(d, dict):
            return None
        try:
            # Bitunix reports fundingRate in PERCENT (e.g. 0.0078 == 0.0078%); convert to a fraction.
            return float(d.get("fundingRate")) / 100.0
        except (TypeError, ValueError):
            return None
