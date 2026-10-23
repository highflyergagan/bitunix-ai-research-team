"""SQLite persistence (WAL mode, single-writer lock). Survives restarts."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
  status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
  qty REAL NOT NULL CHECK (qty > 0), signal_price REAL NOT NULL, entry_price REAL NOT NULL,
  stop_loss REAL NOT NULL, take_profit REAL NOT NULL, risk_usd REAL NOT NULL,
  opened_at INTEGER NOT NULL, last_checked_ms INTEGER NOT NULL,
  mark_price REAL, closed_at INTEGER, exit_price REAL, exit_reason TEXT,
  entry_fee REAL NOT NULL DEFAULT 0, exit_fee REAL NOT NULL DEFAULT 0, funding_paid REAL NOT NULL DEFAULT 0,
  gross_pnl REAL, net_pnl REAL, signal_id INTEGER, ai_decision_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_symbol ON positions(symbol) WHERE status='OPEN';
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL, finished_at INTEGER,
  status TEXT NOT NULL, symbols_scanned INTEGER DEFAULT 0, signals INTEGER DEFAULT 0,
  candidates INTEGER DEFAULT 0, opened INTEGER DEFAULT 0, closed INTEGER DEFAULT 0,
  api_requests INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, ts INTEGER NOT NULL, symbol TEXT NOT NULL,
  bias TEXT NOT NULL, score INTEGER, entry REAL, stop_loss REAL, take_profit REAL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS ai_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, symbol TEXT NOT NULL, signal_id INTEGER,
  decision TEXT NOT NULL, confidence REAL, reasons TEXT, model TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER NOT NULL, balance REAL NOT NULL, equity REAL NOT NULL, open_positions INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    # --- helpers ---
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store.lock.acquire()
                store.conn.execute("BEGIN IMMEDIATE")
                return store

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    # --- key/value state ---
    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def log(self, level: str, message: str, ts: int | None = None) -> None:
        self.execute("INSERT INTO events(ts,level,message) VALUES(?,?,?)", (ts or now_ms(), level, message[:2000]))
