"""Manual paper desk -- the user's own discretionary ideas, PAPER ONLY, in a separate $100 ledger.

Entries go through the same risk engine as every other ledger (0.5% risk incl. costs, valid SL/TP,
reward:risk after costs, daily/drawdown budgets, max 2 open). Exits come only from real later 1m candles.
The entry price is the current Bitunix market price; no exchange order exists anywhere.
"""
from __future__ import annotations

import dataclasses
import os

from .bitunix import INTERVAL_MS
from .config import Settings
from .paper import PaperBroker
from .risk import RiskRejected
from .store import Store, now_ms


class ManualDesk:
    def __init__(self, settings: Settings, client, candles_since, clock=now_ms):
        self.client = client
        self.candles_since = candles_since
        cfg = dataclasses.replace(settings, min_notional=1.0)
        self.broker = PaperBroker(Store(os.path.join(settings.data_dir, "manual_paper.db")), cfg, clock)

    def open(self, symbol: str, side: str, stop_loss: float, take_profit: float, trail_pct: float | None = None,
             note: str = "") -> dict:
        symbol, side = symbol.upper().strip(), side.upper().strip()
        if side not in ("LONG", "SHORT"):
            raise RiskRejected("side must be LONG or SHORT")
        price = None
        for x in self.client.tickers():
            if x.get("symbol") == symbol:
                price = float(x.get("lastPrice") or 0)
        if not price:
            raise RiskRejected(f"{symbol} not found in Bitunix tickers")
        sig = {"symbol": symbol, "bias": "LONG BIAS" if side == "LONG" else "SHORT BIAS", "entry": price,
               "stop_loss": float(stop_loss), "take_profit": float(take_profit),
               "trail_pct": float(trail_pct) if trail_pct else None}
        p = self.broker.open_position(sig, market_price=price)
        self.broker.store.log("TRADE", f"manual paper idea: {side} {symbol} {note}".strip())
        return p

    def manage(self, prices: dict[str, float]) -> int:
        closed = 0
        self.broker.roll_day_and_limits()
        for p in self.broker.open_positions():
            cs = self.candles_since(p["symbol"], p["last_checked_ms"])
            after = self.broker.process_candles(p, cs, INTERVAL_MS["1m"], self.client.funding_rate(p["symbol"]))
            if after["status"] == "CLOSED":
                closed += 1
            elif prices.get(p["symbol"]):
                self.broker.store.execute("UPDATE positions SET mark_price=? WHERE id=?", (prices[p["symbol"]], p["id"]))
        self.broker.record_equity()
        return closed

    def summary(self) -> dict:
        return {"account": self.broker.account(), "open_positions": self.broker.open_positions(),
                "closed_positions": self.broker.closed_positions(50)}
