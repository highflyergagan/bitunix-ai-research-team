"""Simulated (paper) account. No exchange orders are ever created.

Exits are driven only by real subsequent market candles: a position closes when a
later candle's high/low actually crosses the stop or target. If both are touched in
the same candle we assume the stop was hit first (conservative). Gaps through a level
fill at the candle open, not at the level.
"""
from __future__ import annotations

import datetime as dt

from .config import LIVE_TRADING_ENABLED, Settings
from .risk import RiskRejected, fill_price, pre_trade_checks, size_position, slippage_for
from .store import Store, now_ms

FUNDING_HOURS_UTC = (0, 8, 16)


def utc_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def funding_times_between(start_ms: int, end_ms: int) -> list[int]:
    """Bitunix funding timestamps (every 8h at 00/08/16 UTC) in (start, end]."""
    out = []
    t = (start_ms // 28_800_000 + 1) * 28_800_000
    while t <= end_ms:
        out.append(t)
        t += 28_800_000
    return out


class PaperBroker:
    def __init__(self, store: Store, settings: Settings, clock=now_ms):
        if LIVE_TRADING_ENABLED:
            raise RuntimeError("Paper broker refuses to run with live trading enabled")
        self.store = store
        self.s = settings
        self.clock = clock
        if self.store.get("balance") is None:
            self.reset()

    # ---------- account state ----------
    def reset(self) -> None:
        with self.store.transaction() as st:
            st.conn.execute("DELETE FROM positions")
            st.conn.execute("DELETE FROM equity")
        t = self.clock()
        self.store.set("balance", self.s.starting_balance)
        self.store.set("starting_balance", self.s.starting_balance)
        self.store.set("peak_equity", self.s.starting_balance)
        self.store.set("day", utc_day(t))
        self.store.set("day_start_equity", self.s.starting_balance)
        self.store.set("halted", None)
        self.store.set("account_created_at", t)
        self.store.log("INFO", f"Paper account reset to {self.s.starting_balance:.2f} USDT", t)

    def open_positions(self) -> list[dict]:
        return self.store.query("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at")

    def closed_positions(self, limit: int = 200) -> list[dict]:
        return self.store.query("SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?", (limit,))

    @staticmethod
    def unrealized(p: dict, mark: float | None, s: Settings) -> float:
        mark = mark or p.get("mark_price") or p["entry_price"]
        sign = 1 if p["side"] == "LONG" else -1
        gross = sign * (mark - p["entry_price"]) * p["qty"]
        est_exit_fee = mark * p["qty"] * s.taker_fee
        return gross - p["entry_fee"] - est_exit_fee - p["funding_paid"]

    def account(self) -> dict:
        balance = self.store.get("balance")
        opens = self.open_positions()
        upnl = sum(self.unrealized(p, None, self.s) for p in opens)
        equity = balance + upnl
        closed = self.store.query("SELECT net_pnl FROM positions WHERE status='CLOSED'")
        wins = sum(1 for c in closed if c["net_pnl"] > 0)
        start = self.store.get("starting_balance")
        peak = max(self.store.get("peak_equity"), equity)
        return {
            "mode": "PAPER TRADING ONLY",
            "live_trading_enabled": LIVE_TRADING_ENABLED,
            "starting_balance": start,
            "balance": round(balance, 4),
            "unrealized_pnl": round(upnl, 4),
            "equity": round(equity, 4),
            "realized_pnl": round(balance - start, 4),
            "total_return_pct": round((equity / start - 1) * 100, 3),
            "peak_equity": round(peak, 4),
            "drawdown_pct": round((1 - equity / peak) * 100, 3) if peak else 0,
            "day": self.store.get("day"),
            "day_start_equity": round(self.store.get("day_start_equity"), 4),
            "day_pnl": round(equity - self.store.get("day_start_equity"), 4),
            "halted": self.store.get("halted"),
            "open_positions": len(opens),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
        }

    def roll_day_and_limits(self) -> dict:
        """Update day boundary, peak equity, and kill switches. Returns account."""
        acct = self.account()
        t = self.clock()
        today = utc_day(t)
        if self.store.get("day") != today:
            self.store.set("day", today)
            self.store.set("day_start_equity", acct["equity"])
            halted = self.store.get("halted")
            if halted and halted.startswith("daily"):
                self.store.set("halted", None)
            self.store.log("INFO", f"New UTC day {today}; day-start equity {acct['equity']:.2f}", t)
        if acct["equity"] > self.store.get("peak_equity"):
            self.store.set("peak_equity", acct["equity"])
        acct = self.account()
        if not acct["halted"]:
            if acct["equity"] <= acct["peak_equity"] * (1 - self.s.max_drawdown):
                self.store.set("halted", f"max drawdown {self.s.max_drawdown:.0%} reached -- manual reset required")
                self.store.log("ERROR", "Max drawdown kill switch triggered; closing all paper positions", t)
                self.close_all("drawdown kill switch")
            elif acct["equity"] <= acct["day_start_equity"] * (1 - self.s.max_daily_loss):
                self.store.set("halted", f"daily loss limit {self.s.max_daily_loss:.0%} reached -- resumes next UTC day")
                self.store.log("WARN", "Daily loss limit reached; new entries blocked until next UTC day", t)
        return self.account()

    def record_equity(self) -> None:
        a = self.account()
        self.store.execute("INSERT INTO equity(ts,balance,equity,open_positions) VALUES(?,?,?,?)",
                           (self.clock(), a["balance"], a["equity"], a["open_positions"]))

    # ---------- entries ----------
    def check(self, symbol: str, side: str) -> list[str]:
        acct = self.roll_day_and_limits()
        t = self.clock()
        day_start_ms = int(dt.datetime.strptime(utc_day(t), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        trades_today = self.store.one("SELECT COUNT(*) n FROM positions WHERE opened_at>=?", (day_start_ms,))["n"]
        last = self.store.one("SELECT MAX(closed_at) t FROM positions WHERE symbol=? AND status='CLOSED'", (symbol,))
        return pre_trade_checks(symbol=symbol, side=side, account=acct, open_positions=self.open_positions(),
                                trades_today=trades_today, last_close_ms_for_symbol=last["t"] if last else None,
                                now_ms=t, s=self.s)

    def open_position(self, signal: dict, *, signal_id: int | None = None, ai_decision_id: int | None = None,
                      market_price: float | None = None) -> dict:
        side = {"LONG BIAS": "LONG", "SHORT BIAS": "SHORT"}.get(signal.get("bias"))
        if side is None:
            raise RiskRejected("signal is NO TRADE")
        symbol = signal["symbol"]
        reasons = self.check(symbol, side)
        if reasons:
            raise RiskRejected("; ".join(reasons))
        entry = market_price or signal["entry"]
        stop, tp = signal["stop_loss"], signal["take_profit"]
        # If price moved since the signal, levels must still be on the right side.
        acct = self.account()
        used = sum(p["qty"] * p["entry_price"] for p in self.open_positions())
        available = acct["equity"] * self.s.max_leverage - used
        sz = size_position(symbol, side, entry, stop, tp, acct["equity"], available, self.s)
        t = self.clock()
        entry_fee = sz.notional * self.s.taker_fee
        with self.store.transaction() as st:
            # re-check duplicates inside the transaction (unique index also enforces this)
            n = st.conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
            if n >= self.s.max_open_positions:
                raise RiskRejected("max open positions reached")
            cur = st.conn.execute(
                """INSERT INTO positions(symbol,side,status,qty,signal_price,entry_price,stop_loss,take_profit,risk_usd,
                   opened_at,last_checked_ms,mark_price,entry_fee,signal_id,ai_decision_id)
                   VALUES(?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, side, sz.qty, entry, sz.fill_entry, stop, tp, sz.risk_usd, t, t, sz.fill_entry, entry_fee,
                 signal_id, ai_decision_id))
            pid = cur.lastrowid
        self.store.log("TRADE", f"OPEN {side} {symbol} qty={sz.qty:.6g} @ {sz.fill_entry:.6g} SL={stop} TP={tp} "
                                f"risk={sz.risk_usd:.3f} USDT R:R={sz.reward_risk:.2f} (paper)", t)
        return self.store.one("SELECT * FROM positions WHERE id=?", (pid,))

    # ---------- exits ----------
    def close_position(self, p: dict, raw_price: float, reason: str, ts: int, *, slipped: bool = True) -> dict:
        slip = slippage_for(p["symbol"], self.s) if slipped else 0.0
        exit_price = fill_price(raw_price, p["side"], False, slip)
        sign = 1 if p["side"] == "LONG" else -1
        gross = sign * (exit_price - p["entry_price"]) * p["qty"]
        exit_fee = exit_price * p["qty"] * self.s.taker_fee
        net = gross - p["entry_fee"] - exit_fee - p["funding_paid"]
        with self.store.transaction() as st:
            cur = st.conn.execute(
                """UPDATE positions SET status='CLOSED', closed_at=?, exit_price=?, exit_reason=?, exit_fee=?,
                   gross_pnl=?, net_pnl=?, mark_price=? WHERE id=? AND status='OPEN'""",
                (ts, exit_price, reason, exit_fee, gross, net, exit_price, p["id"]))
            if cur.rowcount != 1:
                return p  # already closed by someone else -- never double-count
            bal = json_get(st, "balance")
            st.conn.execute("UPDATE kv SET value=? WHERE key='balance'", (repr(bal + net),))
        self.store.log("TRADE", f"CLOSE {p['side']} {p['symbol']} @ {exit_price:.6g} ({reason}) net={net:+.4f} USDT", ts)
        return self.store.one("SELECT * FROM positions WHERE id=?", (p["id"],))

    def close_all(self, reason: str) -> None:
        t = self.clock()
        for p in self.open_positions():
            self.close_position(p, p["mark_price"] or p["entry_price"], reason, t)

    def process_candles(self, p: dict, candles: list[dict], interval_ms: int,
                        funding_rate: float | None = None) -> dict:
        """Walk real candles that started at/after the position was opened, in time order."""
        t = self.clock()
        checked = p["last_checked_ms"]
        funding_paid = p["funding_paid"]
        mark = p["mark_price"]
        for c in candles:
            start, end = c["time"], c["time"] + interval_ms
            if start < p["opened_at"] or end <= checked:
                continue  # candle began before entry or was already processed
            # funding settlements that fell before this candle ended
            # (only for fully closed candles so a re-processed open candle is never charged twice)
            if funding_rate is not None and end <= t:
                for _ in funding_times_between(max(checked, p["opened_at"]), end):
                    sign = 1 if p["side"] == "LONG" else -1
                    funding_paid += sign * funding_rate * c["open"] * p["qty"]
            hit_stop = c["low"] <= p["stop_loss"] if p["side"] == "LONG" else c["high"] >= p["stop_loss"]
            hit_tp = c["high"] >= p["take_profit"] if p["side"] == "LONG" else c["low"] <= p["take_profit"]
            if hit_stop or hit_tp:
                self.store.execute("UPDATE positions SET funding_paid=? WHERE id=?", (funding_paid, p["id"]))
                p = dict(p, funding_paid=funding_paid)
                if hit_stop:
                    gapped = c["open"] <= p["stop_loss"] if p["side"] == "LONG" else c["open"] >= p["stop_loss"]
                    price = c["open"] if gapped else p["stop_loss"]
                    reason = "stop-loss (gap)" if gapped else ("stop-loss" if not hit_tp else "stop-loss (SL+TP same candle, assumed SL)")
                    return self.close_position(p, price, reason, min(end, t))
                gapped = c["open"] >= p["take_profit"] if p["side"] == "LONG" else c["open"] <= p["take_profit"]
                return self.close_position(p, c["open"] if gapped else p["take_profit"], "take-profit", min(end, t))
            mark = c["close"]
            if end <= t:
                checked = end  # only advance past fully closed candles
        # time stop
        if t - p["opened_at"] >= self.s.max_hold_hours * 3_600_000 and mark:
            self.store.execute("UPDATE positions SET funding_paid=? WHERE id=?", (funding_paid, p["id"]))
            return self.close_position(dict(p, funding_paid=funding_paid), mark, f"time stop ({self.s.max_hold_hours:g}h)", t)
        self.store.execute("UPDATE positions SET last_checked_ms=?, mark_price=?, funding_paid=? WHERE id=?",
                           (checked, mark, funding_paid, p["id"]))
        return self.store.one("SELECT * FROM positions WHERE id=?", (p["id"],))


def json_get(st: Store, key: str):
    import json
    row = st.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0])
