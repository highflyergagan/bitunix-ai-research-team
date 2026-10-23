"""Study mode: watch extreme pump/dump coins -- RESEARCH ONLY, never trades.

Each scan already downloads every Bitunix ticker. From those we:
  1. record a PUMP (24h change >= +threshold) or DUMP (<= -threshold) event once per symbol per UTC day;
  2. keep following that coin's real price on every later scan (max/min since detection);
  3. snapshot the price 1h, 4h and 24h after detection.
The summary answers: after a big pump, did the price usually keep going or reverse?
"""
from __future__ import annotations

import datetime as dt
import statistics

from .store import Store, now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS study_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('PUMP','DUMP')), day TEXT NOT NULL,
  detected_at INTEGER NOT NULL, price0 REAL NOT NULL, change_24h REAL NOT NULL, quote_volume REAL,
  high_24h REAL, low_24h REAL, max_price REAL NOT NULL, min_price REAL NOT NULL,
  last_price REAL NOT NULL, last_ts INTEGER NOT NULL, p1h REAL, p4h REAL, p24h REAL,
  UNIQUE(symbol, day, kind)
);
"""
HORIZONS = {"p1h": 3_600_000, "p4h": 14_400_000, "p24h": 86_400_000}


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


class StudyMode:
    def __init__(self, store: Store, min_change: float = 0.30, min_volume: float = 200_000.0, clock=now_ms):
        self.store = store
        self.min_change = min_change
        self.min_volume = min_volume
        self.clock = clock
        self.store.conn.executescript(SCHEMA)

    def update(self, tickers: list[dict]) -> dict:
        t = self.clock()
        day = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        prices: dict[str, float] = {}
        new = 0
        with_open = 0
        for x in tickers:
            sym = x.get("symbol")
            last = _f(x.get("lastPrice") or x.get("last"))
            if not sym or not last or last <= 0:
                continue
            prices[sym] = last
            o = _f(x.get("open") or x.get("openPrice") or x.get("open24h"))
            if o:
                with_open += 1
            vol = _f(x.get("quoteVol")) or 0.0
            if not o or o <= 0 or vol < self.min_volume:
                continue
            chg = last / o - 1
            kind = "PUMP" if chg >= self.min_change else "DUMP" if chg <= -self.min_change else None
            if not kind:
                continue
            cur = self.store.execute(
                "INSERT OR IGNORE INTO study_events(symbol,kind,day,detected_at,price0,change_24h,quote_volume,high_24h,"
                "low_24h,max_price,min_price,last_price,last_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sym, kind, day, t, last, chg, vol, _f(x.get("high")), _f(x.get("low")), last, last, last, t))
            if cur.rowcount:
                new += 1
                self.store.log("STUDY", f"{kind} detected: {sym} {chg:+.0%} in 24h @ {last:g} (research only, no trade)", t)
        # follow every event still inside its 24h window
        followed = 0
        for e in self.store.query("SELECT * FROM study_events WHERE p24h IS NULL"):
            p = prices.get(e["symbol"])
            if p is None:
                continue
            upd = {"max_price": max(e["max_price"], p), "min_price": min(e["min_price"], p),
                   "last_price": p, "last_ts": t}
            for col, h in HORIZONS.items():
                if e[col] is None and t - e["detected_at"] >= h:
                    upd[col] = p
            sets = ", ".join(f"{k}=?" for k in upd)
            self.store.execute(f"UPDATE study_events SET {sets} WHERE id=?", (*upd.values(), e["id"]))
            followed += 1
        self.store.set("study_last_update", {"ts": t, "tickers": len(tickers), "with_24h_open": with_open,
                                             "new_events": new, "followed": followed})
        return {"new_events": new, "followed": followed}

    def summary(self, limit: int = 100) -> dict:
        rows = self.store.query("SELECT * FROM study_events ORDER BY detected_at DESC LIMIT ?", (limit,))
        events = []
        for e in rows:
            p0 = e["price0"]
            ret = lambda v: None if v is None else round((v / p0 - 1) * 100, 2)  # noqa: E731
            events.append({
                "symbol": e["symbol"], "kind": e["kind"], "detected_at": e["detected_at"],
                "change_24h_pct": round(e["change_24h"] * 100, 1), "price_at_detection": p0,
                "quote_volume_24h": e["quote_volume"],
                "after_1h_pct": ret(e["p1h"]), "after_4h_pct": ret(e["p4h"]), "after_24h_pct": ret(e["p24h"]),
                "now_pct": ret(e["last_price"]),
                "best_since_pct": ret(e["max_price"]), "worst_since_pct": ret(e["min_price"]),
            })
        stats = {}
        for kind in ("PUMP", "DUMP"):
            k = {}
            for col in ("after_1h_pct", "after_4h_pct", "after_24h_pct"):
                vals = [ev[col] for ev in events if ev["kind"] == kind and ev[col] is not None]
                if vals:
                    k[col] = {"n": len(vals), "median": round(statistics.median(vals), 2),
                              "avg": round(sum(vals) / len(vals), 2),
                              "pct_kept_going": round(sum(1 for v in vals if (v > 0) == (kind == "PUMP")) / len(vals) * 100, 1)}
            stats[kind] = {"events": sum(1 for ev in events if ev["kind"] == kind), "forward": k}
        # "short it after a pump" -- hypothetical, before fees, no trade is placed
        pumps = [ev for ev in events if ev["kind"] == "PUMP"]
        short = {}
        for col in ("after_1h_pct", "after_4h_pct", "after_24h_pct"):
            vals = [-ev[col] for ev in pumps if ev[col] is not None]
            if vals:
                short[col.replace("after_", "short_")] = {
                    "n": len(vals), "median_return_pct": round(statistics.median(vals), 2),
                    "win_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)}
        squeezes = [ev["best_since_pct"] for ev in pumps if ev["best_since_pct"] is not None]
        if squeezes:
            short["worst_squeeze_against_short_pct"] = max(squeezes)
            short["median_max_rise_after_detection_pct"] = round(statistics.median(squeezes), 2)
        stats["SHORT_AFTER_PUMP"] = short
        return {"mode": "STUDY ONLY -- no trades are placed from this data",
                "rules": {"pump_or_dump_threshold_pct": round(self.min_change * 100), "min_quote_volume": self.min_volume},
                "last_update": self.store.get("study_last_update"),
                "stats": stats, "events": events,
                "short_after_pump_note": "Hypothetical: shorting each PUMP at the price it was detected. Before fees, "
                                         "funding and slippage. 'worst squeeze' = how far price rose AFTER detection "
                                         "(what a short would have had to survive).",
                "note": "Small samples are noise. Look for consistent patterns across weeks, not single days."}
