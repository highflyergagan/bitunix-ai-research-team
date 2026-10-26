"""My real trades -- a journal the user fills in by hand. RECORD ONLY: nothing here talks to an exchange.

Stored in data/journal.db (stays on the user's computer; never committed). Stats compare the user's own
results with the backtests: win rate, average win/loss, profit factor, total P&L.
"""
from __future__ import annotations

import os

from .store import Store, now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, closed_at TEXT, symbol TEXT NOT NULL,
  side TEXT NOT NULL, leverage REAL, pump_pct REAL, entry REAL, exit REAL, qty REAL, pnl REAL NOT NULL,
  fee REAL DEFAULT 0, setup TEXT, notes TEXT
);
"""


class Journal:
    def __init__(self, data_dir: str, clock=now_ms):
        self.store = Store(os.path.join(data_dir, "journal.db"))
        self.store.conn.executescript(SCHEMA)
        self.clock = clock

    def add(self, *, symbol: str, side: str, pnl: float, closed_at: str = "", leverage: float | None = None,
            pump_pct: float | None = None, entry: float | None = None, exit: float | None = None,
            qty: float | None = None, fee: float = 0.0, setup: str = "", notes: str = "") -> dict:
        side = side.upper().strip()
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        symbol = symbol.upper().strip()
        if not symbol or len(symbol) > 24 or not symbol.isalnum():
            raise ValueError("invalid symbol")
        if entry is None and exit and qty:  # derive entry from realised P&L (ignores fees/funding)
            entry = exit + pnl / qty if side == "SHORT" else exit - pnl / qty
        rid = self.store.execute(
            "INSERT INTO journal(created_at,closed_at,symbol,side,leverage,pump_pct,entry,exit,qty,pnl,fee,setup,notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), closed_at[:40], symbol, side, leverage, pump_pct, entry, exit, qty, float(pnl), float(fee or 0),
             setup[:80], notes[:500])).lastrowid
        return self.store.one("SELECT * FROM journal WHERE id=?", (rid,))

    def delete(self, rid: int) -> bool:
        return self.store.execute("DELETE FROM journal WHERE id=?", (rid,)).rowcount == 1

    def summary(self) -> dict:
        rows = self.store.query("SELECT * FROM journal ORDER BY id DESC")
        nets = [r["pnl"] - (r["fee"] or 0) for r in rows]
        wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
        gl = -sum(losses)
        return {"trades": rows, "stats": {
            "count": len(rows), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(rows) * 100, 1) if rows else None,
            "net_pnl": round(sum(nets), 4), "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
            "profit_factor": round(sum(wins) / gl, 2) if gl else None,
            "note": "Record only. Fewer than 30 trades says little about an edge."}}
