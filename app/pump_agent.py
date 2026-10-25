"""100% Pump Exhaustion SHORT Agent -- PAPER ONLY, its own separate ledger (data/agent_pump_exhaustion.db).

What it does each scan
  1. finds Bitunix USDT perpetuals up >= threshold (default +100%) over the rolling 24h ticker window
  2. verifies the contract (listed in tickers, fresh gap-free 15m history), liquidity (24h volume, order-book spread
     and depth) and the funding rate (squeeze check)
  3. reads exhaustion evidence from closed 15m candles: abnormal volume, volume fading after the top, RSI bearish
     divergence, liquidity sweep / failed breakout, lower highs, bearish market-structure shift
  4. MANDATORY: a confirmed 15m breakdown below a swing low AND a failed retest of that level on the latest candle
  5. stop = most recent swing high (the structure that would invalidate the idea) + 0.5 x ATR;
     target chosen so reward:risk is >= 2.0 AFTER fees + slippage; target may not sit below the pre-pump price
  6. SHORT or NO TRADE, always with reasons. Every evaluation is stored.
  7. Execution gate: strategy must have PASSED the walk-forward backtest (see pump_backtest.py), then the
     deterministic Risk Agent pre-checks, then the AI Research Committee (Claude departments + Grok independent
     review + Chief), then the risk engine again at entry. Any failure => no trade. Until validated it runs in
     SHADOW mode: signals are recorded, nothing is executed and no AI budget is spent.

No exchange orders exist anywhere in this code. Real-money trading stays disabled.
"""
from __future__ import annotations

import dataclasses
import json
import os
import statistics

from . import indicators as ind
from .bitunix import INTERVAL_MS
from .config import LIVE_TRADING_ENABLED, Settings
from .paper import PaperBroker
from .risk import RiskRejected
from .store import Store, now_ms
from .strategy import NO_TRADE, SHORT

M15 = INTERVAL_MS["15m"]
NAME = "100% Pump Exhaustion SHORT Agent"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pump_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, symbol TEXT NOT NULL, change_24h REAL,
  decision TEXT NOT NULL, action TEXT NOT NULL, entry REAL, stop_loss REAL, take_profit REAL,
  reasons TEXT, blockers TEXT, evidence TEXT, ai TEXT
);
CREATE INDEX IF NOT EXISTS pump_eval_ts ON pump_evaluations(ts);
"""

# Default parameters (shared by the live agent and the backtest so both test exactly the same rules).
PARAMS = {
    "wing": 2,                    # swing pivot = higher/lower than 2 candles each side
    "breakdown_buffer": 0.002,    # close must be 0.2% below the level to count as a breakdown
    "retest_tolerance": 0.004,    # retest = high comes back within 0.4% of the broken level
    "retest_max_bars": 8,         # retest must come within 2h of the breakdown
    "stop_atr_mult": 0.5,
    "min_stop_pct": 0.006,
    "max_stop_pct": 0.25,
    "min_net_rr": 2.0,
    "min_confluence": 2,          # at least 2 exhaustion signs besides the breakdown + retest
    "min_off_top": 0.05,          # within 5% of the top = squeeze risk
    "max_1h_rise": 0.08,          # price up > 8% in the last hour = squeeze risk
    "min_funding": -0.001,        # funding <= -0.10% per 8h => shorts crowded => squeeze risk
    "max_spread": 0.003,          # 0.30%
    "min_book_usd": 10_000.0,     # top-5 bid notional must be at least this
    "slippage": 0.005,            # 0.5% per fill on pumped coins
    "fee": 0.0006,
}


# ---------------------------------------------------------------- indicators
def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def pivots(c: list[dict], wing: int) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    for i in range(wing, len(c) - wing):
        win = c[i - wing:i + wing + 1]
        if c[i]["high"] >= max(x["high"] for x in win):
            highs.append(i)
        if c[i]["low"] <= min(x["low"] for x in win):
            lows.append(i)
    return highs, lows


def short_net_rr(entry: float, stop: float, tp: float, slip: float, fee: float) -> float:
    fe = entry * (1 - slip)
    sf, tf = stop * (1 + slip), tp * (1 + slip)
    loss = sf - fe + fee * (fe + sf)
    reward = fe - tf - fee * (fe + tf)
    return reward / loss if loss > 0 else 0.0


# ---------------------------------------------------------------- the signal
def evaluate(symbol: str, c15: list[dict], *, change_24h: float | None = None, threshold: float = 1.0,
             funding_rate: float | None = None, book: dict | None = None, quote_volume: float | None = None,
             min_quote_volume: float = 2_000_000.0, now: int | None = None, live_checks: bool = True,
             p: dict | None = None) -> dict:
    """Deterministic SHORT / NO TRADE decision from CLOSED 15m candles. Pure function (used live and in backtests)."""
    p = {**PARAMS, **(p or {})}
    sig = {"symbol": symbol, "bias": NO_TRADE, "entry": None, "stop_loss": None, "take_profit": None,
           "reasons": [], "blockers": [], "evidence": {}, "indicators": {},
           "data_gaps": ["verified news / catalyst feed (not connected)", "open interest", "token-unlock calendar"],
           "score": 0,
           "change_24h": change_24h, "strategy": "pump_exhaustion"}
    B, R, E = sig["blockers"], sig["reasons"], sig["evidence"]

    def done():
        if not B and sig["entry"] is not None:
            sig["bias"] = SHORT
        return sig

    n = len(c15)
    if n < 110:
        B.append(f"insufficient 15m history ({n} candles, need 110)")
        return done()
    for a, b in zip(c15[-100:], c15[-99:]):
        if b["time"] - a["time"] != M15:
            B.append("15m price history has gaps")
            return done()
    if now is not None and now - (c15[-1]["time"] + M15) > 2 * M15:
        B.append("stale data: last closed 15m candle is older than 30 minutes")
        return done()

    o = [x["open"] for x in c15]
    h = [x["high"] for x in c15]
    l = [x["low"] for x in c15]
    cl = [x["close"] for x in c15]
    v = [x["quote_volume"] for x in c15]
    last_i = n - 1
    last = cl[-1]
    base_price = cl[-97]  # price 24h ago (pre-pump reference)
    chg = change_24h if change_24h is not None else last / base_price - 1
    sig["change_24h"] = chg
    if chg < threshold:
        B.append(f"24h change {chg:+.0%} below the {threshold:+.0%} pump threshold")
        return done()
    R.append(f"pumped {chg:+.0%} in 24h")

    w0 = n - 96
    top_i = max(range(w0, n), key=lambda i: h[i])
    top = h[top_i]
    off_top = 1 - last / top
    E["top"], E["off_top_pct"] = top, round(off_top * 100, 2)

    # --- liquidity / data / squeeze gates
    if quote_volume is not None and quote_volume < min_quote_volume:
        B.append(f"low liquidity: 24h volume {quote_volume:,.0f} USDT < {min_quote_volume:,.0f}")
    if live_checks:
        if book is None:
            B.append("order book unavailable (spread/depth is mandatory evidence)")
        else:
            E["spread_pct"] = round(book["spread"] * 100, 3)
            E["bid_depth_usd"] = round(book["bid_depth_usd"])
            if book["spread"] > p["max_spread"]:
                B.append(f"spread {book['spread']:.2%} above {p['max_spread']:.2%}")
            if book["bid_depth_usd"] < p["min_book_usd"]:
                B.append(f"thin order book: top-5 bids {book['bid_depth_usd']:,.0f} USDT")
        if funding_rate is None:
            B.append("funding rate unavailable (mandatory squeeze check)")
    if funding_rate is not None:
        E["funding_rate"] = funding_rate
        if funding_rate <= p["min_funding"]:
            B.append(f"squeeze risk: funding {funding_rate:.3%} -- shorts are crowded")
    if off_top < p["min_off_top"]:
        B.append(f"squeeze risk: price only {off_top:.1%} below the 24h top")
    rise_1h = last / cl[-5] - 1
    if rise_1h > p["max_1h_rise"]:
        B.append(f"squeeze risk: up {rise_1h:.1%} in the last hour")

    # --- exhaustion evidence
    prior = v[:w0] if w0 >= 20 else v
    med = statistics.median(prior) or 1e-12
    peak_vol = max(v[w0:])
    E["abnormal_volume"] = peak_vol / med >= 3.0
    E["peak_volume_x_median"] = round(peak_vol / med, 1)
    around_top = v[max(w0, top_i - 4):top_i + 5]
    E["volume_fading"] = (statistics.mean(v[-8:]) < 0.6 * statistics.mean(around_top)) if top_i < n - 8 else False
    ph, pl = pivots(c15, p["wing"])
    ph = [i for i in ph if i >= w0]
    rs = rsi_series(cl)
    div = False
    recent_ph = [i for i in ph if i >= n - 48]
    if len(recent_ph) >= 2:
        a, b = recent_ph[-2], recent_ph[-1]
        if rs[a] and rs[b] and h[b] >= h[a] * 0.995 and rs[b] < rs[a] - 2:
            div = True
    E["rsi_bearish_divergence"] = div
    lower_highs = [i for i in ph if i > top_i and h[i] < top * 0.98]
    E["lower_high"] = bool(lower_highs)
    sweep = False
    for a in ph:
        for j in range(a + 1, min(n, a + 13)):
            if h[j] > h[a] and cl[j] < h[a]:
                sweep = True
                break
    rng = h[top_i] - l[top_i]
    if rng > 0 and (h[top_i] - max(o[top_i], cl[top_i])) / rng >= 0.5:
        sweep = True  # long rejection wick at the top
    E["liquidity_sweep_or_failed_breakout"] = sweep

    # --- MANDATORY: confirmed 15m breakdown + failed retest on the latest closed candle
    bd = None
    for k in range(last_i - 1, max(top_i, last_i - p["retest_max_bars"]) - 1, -1):
        lv = [i for i in pl if top_i - 32 <= i < k and i + p["wing"] <= k]
        if not lv:
            continue
        L = l[lv[-1]]
        if cl[k] < L * (1 - p["breakdown_buffer"]) and cl[k - 1] >= L * (1 - p["breakdown_buffer"]):
            if all(cl[j] < L for j in range(k, last_i + 1)) and h[last_i] >= L * (1 - p["retest_tolerance"]) \
                    and cl[last_i] < L:
                bd = (k, L, lv[-1])
            break
    E["breakdown_and_failed_retest"] = bool(bd)
    if bd:
        k, L, Li = bd
        E["breakdown_level"] = L
        E["market_structure_shift"] = Li < top_i or bool(lower_highs)
        R.append(f"15m close broke swing low {L:.6g}, latest candle retested it and failed")
    else:
        E["market_structure_shift"] = False
        B.append("no confirmed 15m breakdown + failed retest yet (mandatory)")

    signs = [("abnormal_volume", "abnormal volume during the pump"), ("volume_fading", "volume fading since the top"),
             ("rsi_bearish_divergence", "RSI bearish divergence"),
             ("liquidity_sweep_or_failed_breakout", "liquidity sweep / failed breakout"),
             ("lower_high", "lower high after the top")]
    got = [txt for key, txt in signs if E.get(key)]
    R.extend(got)
    if E.get("market_structure_shift"):
        R.append("bearish market-structure shift")
    sig["score"] = len(got) + (2 if bd else 0)
    if len(got) < p["min_confluence"]:
        B.append(f"only {len(got)} exhaustion signs (need {p['min_confluence']})")

    # --- levels
    atr = ind.atr(c15[-30:])
    E["atr"] = atr
    if not bd or not atr:
        return done()
    k, L, _ = bd
    structure = [i for i in ph if top_i <= i <= last_i]
    swing_high = h[structure[-1]] if structure else top
    swing_high = max(swing_high, max(h[k:last_i + 1]))
    stop = swing_high + p["stop_atr_mult"] * atr
    dist = (stop - last) / last
    E["stop_distance_pct"] = round(dist * 100, 2)
    if dist > p["max_stop_pct"]:
        B.append(f"stop {dist:.1%} away is too wide")
        return done()
    if dist < p["min_stop_pct"]:
        stop = last * (1 + p["min_stop_pct"])
    tp, rr = None, 0.0
    m = 2.0
    while m <= 5.0:
        cand = last - m * (stop - last)
        if cand <= 0:
            break
        rr = short_net_rr(last, stop, cand, p["slippage"], p["fee"])
        if rr >= p["min_net_rr"]:
            tp = cand
            break
        m += 0.25
    if tp is None:
        B.append(f"cannot reach {p['min_net_rr']}:1 reward:risk after costs")
        return done()
    if tp < base_price:
        B.append(f"target {tp:.6g} would sit below the pre-pump price {base_price:.6g}")
        return done()
    E["net_reward_risk"] = round(rr, 2)
    sig.update(entry=last, stop_loss=stop, take_profit=tp)
    sig["indicators"] = {"change_24h_pct": round(chg * 100, 1), "rsi_15m": round(rs[-1], 1) if rs[-1] else None,
                         "atr_15m": atr, "stop_from": "swing high + 0.5 ATR", "net_reward_risk": round(rr, 2),
                         **{k2: E[k2] for k2 in ("off_top_pct", "peak_volume_x_median") if k2 in E}}
    R.append(f"stop {stop:.6g} (swing high + 0.5 ATR), target {tp:.6g}, {rr:.2f}:1 after costs")
    return done()


def book_from_depth(d: dict | None) -> dict | None:
    try:
        bids = [(float(x[0]), float(x[1])) for x in d["bids"]]
        asks = [(float(x[0]), float(x[1])) for x in d["asks"]]
        bb, ba = max(b[0] for b in bids), min(a[0] for a in asks)
    except Exception:
        return None
    if bb <= 0 or ba <= bb:
        return None
    mid = (bb + ba) / 2
    return {"bid": bb, "ask": ba, "spread": (ba - bb) / mid, "bid_depth_usd": sum(p * q for p, q in bids[:5]),
            "ask_depth_usd": sum(p * q for p, q in asks[:5])}


# ---------------------------------------------------------------- the agent
class PumpExhaustionAgent:
    def __init__(self, settings: Settings, client, reviewer, candles_since, main_store: Store, clock=now_ms):
        if LIVE_TRADING_ENABLED:
            raise RuntimeError("refusing to run with live trading enabled")
        self.s = settings
        self.client = client
        self.reviewer = reviewer
        self.candles_since = candles_since
        self.main_store = main_store
        self.clock = clock
        self.threshold = settings.pump_agent_threshold
        self.store = Store(os.path.join(settings.data_dir, "agent_pump_exhaustion.db"))
        self.store.conn.executescript(SCHEMA)
        # pumped coins are thin: 0.5% slippage per fill, wide stops at 0.5% risk => allow $1 paper notional;
        # the risk engine also enforces >= 2:1 reward:risk after costs for this ledger.
        self.cfg = dataclasses.replace(settings, slippage=0.003, alt_extra_slippage=0.002, min_notional=1.0,
                                       min_reward_risk=max(settings.min_reward_risk, 2.0))
        self.broker = PaperBroker(self.store, self.cfg, clock)

    # ---- validation state (written by the backtest) ----
    def validation(self) -> dict:
        return self.store.get("validation") or {"validated": False, "reason": "walk-forward backtest has not been run"}

    def run(self, tickers: list[dict], prices: dict[str, float]) -> dict:
        res = {"evaluated": 0, "signals": 0, "opened": 0, "closed": 0, "errors": []}
        self._exits(prices, res)
        self.broker.roll_day_and_limits()
        movers = []
        for x in tickers:
            try:
                op, last, vol = float(x.get("open") or 0), float(x.get("lastPrice") or 0), float(x.get("quoteVol") or 0)
            except (TypeError, ValueError):
                continue
            sym = x.get("symbol", "")
            if sym.endswith("USDT") and op > 0 and last > 0 and last / op - 1 >= self.threshold:
                movers.append((last / op - 1, sym, vol))
        val = self.validation()
        for chg, sym, vol in sorted(movers, reverse=True)[: self.s.pump_agent_max_checks]:
            try:
                now = self.clock()
                c15 = [c for c in self.client.klines(sym, "15m", 200) if c["time"] + M15 <= now]
                fr = self.client.funding_rate(sym)
                book = book_from_depth(self.client.depth(sym, 5)) if hasattr(self.client, "depth") else None
                sig = evaluate(sym, c15, change_24h=chg, threshold=self.threshold, funding_rate=fr, book=book,
                               quote_volume=vol, min_quote_volume=self.s.min_quote_volume, now=now)
                res["evaluated"] += 1
                action, ai = "none", None
                if sig["bias"] == SHORT:
                    res["signals"] += 1
                    action, ai = self._maybe_execute(sig, prices, val, res)
                self.store.execute(
                    "INSERT INTO pump_evaluations(ts,symbol,change_24h,decision,action,entry,stop_loss,take_profit,"
                    "reasons,blockers,evidence,ai) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, sym, chg, "SHORT" if sig["bias"] == SHORT else "NO TRADE", action, sig["entry"],
                     sig["stop_loss"], sig["take_profit"], json.dumps(sig["reasons"]), json.dumps(sig["blockers"]),
                     json.dumps(sig["evidence"], default=str), json.dumps(ai) if ai else None))
            except Exception as exc:
                res["errors"].append(f"{sym}: {exc}"[:200])
        self.broker.roll_day_and_limits()
        self.broker.record_equity()
        return res

    def _maybe_execute(self, sig, prices, val, res):
        sym = sig["symbol"]
        if not val.get("validated"):
            return "shadow: strategy not validated by walk-forward backtest -> not executed, no AI call", None
        pre = self.broker.check(sym, "SHORT")
        if pre:
            return "blocked by risk agent: " + "; ".join(pre), None
        if not self.s.pump_agent_ai_review:
            return "blocked: AI review is mandatory for this agent but disabled", None
        dec = self.reviewer.review({**sig, "review_context": {
            "agent": NAME, "playbook": "short a +100% 24h pump only after a confirmed 15m breakdown and failed retest",
            "evidence": sig["evidence"], "backtest_validation": val.get("summary")}}, self.broker.account(), None)
        ai = {"decision": dec["decision"], "confidence": dec.get("confidence"), "reasons": dec.get("reasons", [])[:4]}
        if dec["decision"] != "APPROVE":
            return "rejected by AI committee", ai
        try:
            self.broker.open_position(sig, ai_decision_id=dec.get("id"), market_price=prices.get(sym) or sig["entry"])
            res["opened"] += 1
            self.main_store.log("PUMP", f"{NAME}: opened PAPER SHORT {sym} (validated strategy, AI approved)")
            return "executed (paper)", ai
        except RiskRejected as exc:
            return f"rejected by risk engine: {exc}", ai

    def _exits(self, prices, res):
        for p in self.broker.open_positions():
            try:
                cs = self.candles_since(p["symbol"], p["last_checked_ms"])
                after = self.broker.process_candles(p, cs, INTERVAL_MS["1m"], self.client.funding_rate(p["symbol"]))
                if after["status"] == "CLOSED":
                    res["closed"] += 1
                elif prices.get(p["symbol"]):
                    self.store.execute("UPDATE positions SET mark_price=? WHERE id=?", (prices[p["symbol"]], p["id"]))
            except Exception as exc:
                res["errors"].append(f"{p['symbol']} exit: {exc}"[:200])

    # ---- dashboard ----
    def summary(self, limit: int = 40) -> dict:
        a = self.broker.account()
        rows = self.store.query("SELECT * FROM pump_evaluations ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            for k in ("reasons", "blockers", "evidence", "ai"):
                r[k] = json.loads(r[k]) if r[k] else None
        counts = self.store.one("SELECT COUNT(*) n, SUM(decision='SHORT') shorts, SUM(action='executed (paper)') ex "
                                "FROM pump_evaluations")
        return {"name": NAME, "threshold": self.threshold, "mode": "PAPER ONLY",
                "execution": "enabled (validated)" if self.validation().get("validated") else "shadow (not validated)",
                "validation": self.validation(), "backtest": self.store.get("backtest_status"),
                "backtest_result": self.store.get("backtest_result"),
                "account": a, "open_positions": self.broker.open_positions(),
                "closed_positions": self.broker.closed_positions(50),
                "evaluations": rows, "totals": {"evaluations": counts["n"] or 0, "short_signals": counts["shorts"] or 0,
                                                "executed": counts["ex"] or 0},
                "gates": ["contract listed + fresh gap-free 15m data", "24h volume >= min, spread <= 0.30%, top-5 bids >= $10k",
                          "funding > -0.10% (squeeze)", ">= 5% below top, < 8% rise in last hour (squeeze)",
                          "confirmed 15m breakdown + failed retest", ">= 2 exhaustion signs",
                          "stop = swing high + 0.5 ATR (<= 25%)", ">= 2:1 reward:risk after fees + slippage",
                          "strategy validated by walk-forward backtest", "Risk Agent pre-checks (0.5% risk, 2% day, 5% DD, max 2)",
                          "AI Research Committee: Grok independent review + Chief (Claude) approve",
                          "risk engine re-check at entry"]}
