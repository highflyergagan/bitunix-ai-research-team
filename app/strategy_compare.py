"""Read-only research module: compare four strategy families on Bitunix PUBLIC data, net of costs, out-of-sample.

  1. Pump Exhaustion SHORT   -- results of the pump agent's walk-forward backtest (pump_backtest.py)
  2. Liquidity Sweep Reversal -- wick through a prior swing low/high that closes back inside (new deterministic rule)
  3. Trend Following         -- the main account's existing deterministic signal engine (strategy.generate_signal)
  4. Funding/Basis Arbitrage -- RESEARCH ONLY: current funding snapshot vs round-trip costs. Not backtestable here
                                (no historical funding and no spot prices on the public allow-list).

Plus "AI-assisted vs deterministic": every setup the AI Research Committee (or Claude panel) actually judged in the
live paper run is replayed on later real 15m candles, so approved and rejected setups can be compared. AI is NOT
re-run on history: it would cost money and models may know how history unfolded (look-ahead leakage).

Nothing here trades, activates a strategy, resets data or calls a paid service.
Run: dashboard "Run strategy comparison", POST /api/research/compare, or  python -m app.strategy_compare
"""
from __future__ import annotations

import json
import os
import threading
import traceback

from . import indicators as ind
from .bitunix import INTERVAL_MS, BitunixPublicClient
from .config import Settings, load_dotenv
from .pump_backtest import fetch_range, metrics
from .store import Store, now_ms
from .strategy import LONG, SHORT, generate_signal

M15, H1, DAY = INTERVAL_MS["15m"], INTERVAL_MS["1h"], 86_400_000
FEE = 0.0006
FUNDING_8H = 0.0001  # constant funding charged against EVERY position (historical funding not available)
_lock = threading.Lock()


def slip_for(sym: str) -> float:
    return 0.0005 if sym in ("BTCUSDT", "ETHUSDT") else 0.0010  # same as the main paper account


# ---------------------------------------------------------------- generic trade simulation
def sim_trade(c15: list[dict], e: int, side: str, stop: float, tp: float, slip: float, max_bars: int = 96,
              funding_8h: float = FUNDING_8H) -> dict | None:
    """Enter at c15[e].open; stop checked before target; gaps fill at the open; 24h time stop.
    Returns R net of slippage, fees and funding, or None if the levels are invalid or data ends early."""
    if e >= len(c15):
        return None
    ep = c15[e]["open"]
    long = side == "LONG"
    if not ((stop < ep < tp) if long else (tp < ep < stop)):
        return None
    fe = ep * (1 + slip) if long else ep * (1 - slip)
    sf = stop * (1 - slip) if long else stop * (1 + slip)
    risk = abs(fe - sf) + FEE * (fe + sf)
    last = min(e + max_bars - 1, len(c15) - 1)
    px = reason = None
    j = e
    for j in range(e, last + 1):
        c = c15[j]
        if (c["low"] <= stop) if long else (c["high"] >= stop):
            gap = (c["open"] <= stop) if long else (c["open"] >= stop)
            px, reason = (c["open"] if gap else stop), "stop" + (" (gap)" if gap else "")
            break
        if (c["high"] >= tp) if long else (c["low"] <= tp):
            gap = (c["open"] >= tp) if long else (c["open"] <= tp)
            px, reason = (c["open"] if gap else tp), "target"
            break
    if px is None:
        if last < e + max_bars - 1:
            return None
        px, reason = c15[last]["close"], "24h time stop"
    xf = px * (1 - slip) if long else px * (1 + slip)
    exit_t = c15[j]["time"] + M15
    n_fund = max(0, exit_t // 28_800_000 - c15[e]["time"] // 28_800_000)
    pnl = (xf - fe if long else fe - xf) - FEE * (fe + xf) - funding_8h * ep * n_fund
    return {"entry_time": c15[e]["time"], "exit_time": exit_t, "side": side, "r": round(pnl / risk, 4),
            "reason": reason, "exit_index": j}


# ---------------------------------------------------------------- strategy 2: liquidity sweep reversal
def liquidity_sweep(c15: list[dict], wing: int = 3, lookback: int = 48, rr: float = 2.5) -> dict | None:
    """Last closed candle wicks >= 0.1% through the most extreme swing low (high) of the last 12h, closes back
    inside on the rejection side with a >= 50% wick and >= 1.2x average volume. Stop beyond the wick + 0.25 ATR."""
    n = len(c15)
    if n < lookback + 20:
        return None
    c = c15[-1]
    rng = c["high"] - c["low"]
    if rng <= 0:
        return None
    window = c15[-lookback - 1:-1]
    lows, highs = [], []
    for i in range(wing, len(window) - wing):
        seg = window[i - wing:i + wing + 1]
        if window[i]["low"] <= min(x["low"] for x in seg):
            lows.append(window[i]["low"])
        if window[i]["high"] >= max(x["high"] for x in seg):
            highs.append(window[i]["high"])
    avg_v = sum(x["quote_volume"] for x in c15[-21:-1]) / 20
    if avg_v <= 0 or c["quote_volume"] < 1.2 * avg_v:
        return None
    a = ind.atr(c15[-30:])
    if not a:
        return None
    if lows:
        sl = min(lows)
        if c["low"] < sl * 0.999 and c["close"] > sl and c["close"] > c["open"] and \
                (min(c["open"], c["close"]) - c["low"]) / rng >= 0.5:
            stop = c["low"] - 0.25 * a
            return {"side": "LONG", "stop": stop, "tp": c["close"] + rr * (c["close"] - stop),
                    "why": f"swept swing low {sl:.6g} and closed back above"}
    if highs:
        sh = max(highs)
        if c["high"] > sh * 1.001 and c["close"] < sh and c["close"] < c["open"] and \
                (c["high"] - max(c["open"], c["close"])) / rng >= 0.5:
            stop = c["high"] + 0.25 * a
            return {"side": "SHORT", "stop": stop, "tp": c["close"] - rr * (stop - c["close"]),
                    "why": f"swept swing high {sh:.6g} and closed back below"}
    return None


# ---------------------------------------------------------------- per-symbol backtest of strategies 2 and 3
def backtest_symbol(sym: str, c15: list[dict], c1h: list[dict], min_quote_volume: float = 2_000_000.0) -> dict:
    out = {"liquidity_sweep": [], "trend_following": []}
    slip = slip_for(sym)
    busy = {"liquidity_sweep": -1, "trend_following": -1}
    h_ptr = 0
    for i in range(200, len(c15) - 1):
        t_close = c15[i]["time"] + M15
        # liquidity sweep: every closed 15m candle
        if i > busy["liquidity_sweep"]:
            s = liquidity_sweep(c15[i - 199:i + 1])
            if s:
                tr = sim_trade(c15, i + 1, s["side"], s["stop"], s["tp"], slip)
                if tr:
                    out["liquidity_sweep"].append({"symbol": sym, **tr})
                    busy["liquidity_sweep"] = tr["exit_index"] + 4
        # trend following: the live engine, evaluated at every 1h close
        if t_close % H1 == 0 and i > busy["trend_following"]:
            while h_ptr < len(c1h) and c1h[h_ptr]["time"] + H1 <= t_close:
                h_ptr += 1
            h_win = c1h[max(0, h_ptr - 200):h_ptr]
            if len(h_win) >= 60:
                vol24 = sum(x["quote_volume"] for x in c15[i - 95:i + 1])
                sig = generate_signal(sym, c15[i - 199:i + 1], h_win, quote_volume_24h=vol24, funding_rate=None,
                                      min_quote_volume=min_quote_volume)
                if sig["bias"] in (LONG, SHORT):
                    side = "LONG" if sig["bias"] == LONG else "SHORT"
                    tr = sim_trade(c15, i + 1, side, sig["stop_loss"], sig["take_profit"], slip)
                    if tr:
                        out["trend_following"].append({"symbol": sym, **tr})
                        busy["trend_following"] = tr["exit_index"] + 4
    return out


# ---------------------------------------------------------------- AI-assisted vs deterministic (live paper run)
def ai_decision_replay(main_store: Store, client, cache: Store | None, clock=now_ms) -> dict:
    """Replay every setup the AI actually judged (one per signal) on the real 15m candles that followed."""
    rows = main_store.query(
        "SELECT a.signal_id, a.decision, a.provider, s.ts, s.symbol, s.bias, s.payload FROM ai_decisions a "
        "JOIN signals s ON s.id = a.signal_id WHERE a.model IS NOT NULL AND a.error IS NULL "
        "AND a.decision IN ('APPROVE','REJECT') AND a.reasons NOT LIKE '%already rejected%' "
        "AND a.reasons NOT LIKE '%not re-asking%' ORDER BY a.id")
    final: dict[int, dict] = {}
    for r in rows:  # committee logs several rows per signal; the last (Chief / panel final) one wins
        final[r["signal_id"]] = r
    try:
        for r in main_store.query("SELECT signal_id, decision FROM committee_runs WHERE error IS NULL"):
            if r["signal_id"] in final:
                final[r["signal_id"]] = dict(final[r["signal_id"]], decision=r["decision"])
    except Exception:
        pass
    seen, approved, rejected, skipped = set(), [], [], 0
    now = clock()
    for r in final.values():
        key = (r["symbol"], r["bias"], r["ts"] // H1)
        if key in seen:
            continue
        seen.add(key)
        p = json.loads(r["payload"])
        start = (r["ts"] // M15) * M15
        end = start + 26 * H1
        if end > now:
            skipped += 1
            continue
        c15 = fetch_range(client, r["symbol"], "15m", start, end, cache)
        e = next((k for k, c in enumerate(c15) if c["time"] >= r["ts"]), None)
        tr = sim_trade(c15, e, "LONG" if r["bias"] == LONG else "SHORT", p["stop_loss"], p["take_profit"],
                       slip_for(r["symbol"])) if e is not None else None
        if not tr:
            skipped += 1
            continue
        tr = {"symbol": r["symbol"], "signal_time": r["ts"], **tr}
        (approved if r["decision"] == "APPROVE" else rejected).append(tr)
    m_all = metrics(approved + rejected)
    return {"setups_judged": len(seen), "skipped_not_finished_or_no_data": skipped,
            "ai_approved": metrics(approved), "ai_rejected_counterfactual": metrics(rejected),
            "deterministic_take_all": m_all,
            "note": "Deterministic baseline = take every setup the rules produced. AI value = approved minus take-all. "
                    "Replayed on 15m candles (live account uses 1m), so results can differ slightly from the ledger.",
            "approved_trades": approved, "rejected_trades": rejected}


# ---------------------------------------------------------------- strategy 4: funding / basis (research only)
def funding_snapshot(client, symbols: list[str]) -> dict:
    rows = []
    for s in symbols:
        fr = client.funding_rate(s)
        if fr is None:
            continue
        # delta-neutral carry: short perp + long spot (or reverse). Round trip = 4 fills.
        cost = 2 * (FEE + slip_for(s)) + 2 * (0.001 + slip_for(s))  # perp legs + spot legs (0.1% spot fee assumed)
        per8 = abs(fr)
        rows.append({"symbol": s, "funding_8h_pct": round(fr * 100, 4), "annualised_if_constant_pct": round(per8 * 3 * 365 * 100, 1),
                     "round_trip_cost_pct": round(cost * 100, 3),
                     "settlements_to_break_even": round(cost / per8, 1) if per8 else None})
    rows.sort(key=lambda x: -abs(x["funding_8h_pct"]))
    return {"status": "research only -- not backtested",
            "why_not_backtested": ["Bitunix public allow-list has no historical funding endpoint",
                                   "no spot prices/fees on the allow-list, so the hedge leg and basis cannot be modelled",
                                   "funding flips sign often; a snapshot says nothing about persistence"],
            "snapshot": rows[:15]}


# ---------------------------------------------------------------- ranking
def data_quality(coverage: float, notes: list[str], trades: int) -> dict:
    score = max(0, min(100, int(coverage * 100) - 10 * len(notes) - (20 if trades < 30 else 0)))
    return {"score": score, "grade": "high" if score >= 75 else "medium" if score >= 50 else "low",
            "coverage_pct": round(coverage * 100, 1), "issues": notes}


def rank(entries: list[dict]) -> list[dict]:
    def key(e):
        o = e["out_of_sample"]
        return (o["trades"] > 0, o["expectancy_r"] if o["expectancy_r"] is not None else -99,
                o["profit_factor"] or 0, -(o["max_drawdown_pct"] or 0), o["trades"], e["data_quality"]["score"])
    ranked = sorted([e for e in entries if e.get("backtested")], key=key, reverse=True)
    for i, e in enumerate(ranked, 1):
        o = e["out_of_sample"]
        e["rank"] = i
        e["profitable_out_of_sample"] = bool(o["trades"] >= 30 and (o["expectancy_r"] or 0) > 0 and (o["profit_factor"] or 0) > 1)
    return ranked + [e for e in entries if not e.get("backtested")]


def run_compare(settings: Settings, main_store: Store, pump_store: Store | None, out_store: Store, client=None,
                clock=now_ms, symbols: list[str] | None = None, days: int = 120) -> dict:
    if not _lock.acquire(blocking=False):
        return {"state": "already running"}
    st = {"state": "running", "phase": "universe", "done": 0, "total": 0, "started_at": clock()}
    try:
        out_store.set("compare_status", st)
        client = client or BitunixPublicClient(pause=max(settings.request_pause_seconds, 0.15))
        cache = Store(os.path.join(settings.data_dir, "pump_backtest_cache.db"))
        cache.conn.execute("CREATE TABLE IF NOT EXISTS bt_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        end = (clock() // H1) * H1
        start = end - days * DAY
        split = start + int((end - start) * 0.6)
        if symbols is None:
            tk = [x for x in client.tickers() if str(x.get("symbol", "")).endswith("USDT")]
            tk.sort(key=lambda x: -float(x.get("quoteVol") or 0))
            symbols = list(dict.fromkeys([f for f in settings.favorites if f in {x["symbol"] for x in tk}] +
                                         [x["symbol"] for x in tk[:20]]))
        st.update(total=len(symbols), phase="fetching 15m + 1h history and simulating")
        trades = {"liquidity_sweep": [], "trend_following": []}
        got = expected = 0
        for n, sym in enumerate(symbols, 1):
            try:
                c15 = fetch_range(client, sym, "15m", start - 3 * DAY, end, cache)
                c1h = fetch_range(client, sym, "1h", start - 10 * DAY, end, cache)
                got += len([c for c in c15 if c["time"] >= start])
                expected += (end - start) // M15
                for k, v in backtest_symbol(sym, c15, c1h, settings.min_quote_volume).items():
                    trades[k] += [t for t in v if t["entry_time"] >= start]
            except Exception as exc:
                st.setdefault("symbol_errors", []).append(f"{sym}: {str(exc)[:120]}")
            st["done"] = n
            out_store.set("compare_status", st)
        coverage = got / expected if expected else 0
        entries = []
        names = {"liquidity_sweep": "Liquidity Sweep Reversal", "trend_following": "Trend Following (main engine, rules only)"}
        for k, tr in trades.items():
            ins = [t for t in tr if t["entry_time"] < split]
            oos = [t for t in tr if t["entry_time"] >= split]
            notes = ["survivorship: universe = today's most liquid coins", "constant funding assumption (0.01%/8h paid)",
                     "15m bars: intrabar order unknown (stop assumed first)"]
            entries.append({"key": k, "name": names[k], "backtested": True, "period_days": days,
                            "universe": f"{len(symbols)} most liquid USDT perps + favorites",
                            "costs": "0.06% fee/side, 0.05% (BTC/ETH) or 0.10% slippage/fill, 0.01% funding/8h",
                            "all": metrics(tr), "in_sample": metrics(ins), "out_of_sample": metrics(oos),
                            "data_quality": data_quality(coverage, notes, len(oos))})
        # 1. pump exhaustion from the agent's own walk-forward backtest
        pr = pump_store.get("backtest_result") if pump_store else None
        if pr and pr.get("table"):
            thr = int(round(settings.pump_agent_threshold * 100))
            row = next(r for r in pr["table"] if r["strategy"] == "pump_exhaustion" and r["threshold_pct"] == thr)
            notes = ["survivorship: delisted coins missing", "no historical order book (spread/depth gates off)",
                     "constant funding assumption (0.05%/8h paid by shorts)"]
            entries.append({"key": "pump_exhaustion", "name": f"Pump Exhaustion SHORT (+{thr}%)", "backtested": True,
                            "period_days": pr["period"]["days"], "universe": f"{pr['symbols']} listed USDT perps",
                            "costs": "0.06% fee/side, 0.5% slippage/fill, 0.05% funding/8h",
                            "all": row["all"], "in_sample": row["in_sample"], "out_of_sample": row["out_of_sample"],
                            "walk_forward": pr["walk_forward"]["pump_exhaustion"]["out_of_sample"],
                            "other_thresholds": [{"threshold_pct": r["threshold_pct"], "oos": r["out_of_sample"]}
                                                 for r in pr["table"] if r["strategy"] == "pump_exhaustion"],
                            "existing_reversal_rules": {k2: pr["walk_forward"][k2]["out_of_sample"]
                                                        for k2 in ("pump_fade", "pump_reversal")},
                            "data_quality": data_quality(0.9, notes, row["out_of_sample"]["trades"])})
        else:
            entries.append({"key": "pump_exhaustion", "name": "Pump Exhaustion SHORT", "backtested": False,
                            "status": "pump agent backtest not finished yet -- run it first"})
        st["phase"] = "funding snapshot"
        out_store.set("compare_status", st)
        entries.append({"key": "funding_basis", "name": "Funding/Basis Arbitrage", "backtested": False,
                        **funding_snapshot(client, symbols)})
        st["phase"] = "AI vs deterministic replay"
        out_store.set("compare_status", st)
        ai = ai_decision_replay(main_store, client, cache, clock)
        ranked = rank(entries)
        winners = [e["name"] for e in ranked if e.get("profitable_out_of_sample")]
        result = {"generated_at": clock(), "period": {"start": start, "end": end, "split": split, "days": days},
                  "ranking_rule": "out-of-sample expectancy (R), then profit factor, drawdown, trade count, data quality; "
                                  "'profitable' needs >= 30 OOS trades, expectancy > 0 and profit factor > 1",
                  "headline": ("Profitable out-of-sample after costs: " + ", ".join(winners)) if winners else
                              "NO strategy is profitable out-of-sample after fees, slippage and funding.",
                  "strategies": ranked, "ai_vs_deterministic": ai,
                  "activation": "nothing was activated; research only"}
        out_store.set("compare_result", result)
        st.update(state="done", phase="finished", finished_at=clock())
        out_store.set("compare_status", st)
        return result
    except Exception as exc:
        st.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:400], trace=traceback.format_exc()[-800:],
                  finished_at=clock())
        out_store.set("compare_status", st)
        return st
    finally:
        _lock.release()


def start_background(settings: Settings, main_store: Store, pump_store: Store | None, out_store: Store) -> dict:
    if _lock.locked():
        return {"started": False, "reason": "a comparison is already running"}
    threading.Thread(target=run_compare, args=(settings, main_store, pump_store, out_store),
                     name="strategy-compare", daemon=True).start()
    return {"started": True}


if __name__ == "__main__":
    load_dotenv()
    s = Settings.from_env()
    res = run_compare(s, Store(s.db_path), Store(os.path.join(s.data_dir, "agent_pump_exhaustion.db")),
                      Store(os.path.join(s.data_dir, "strategy_compare.db")))
    print(json.dumps({k: res.get(k) for k in ("headline", "strategies")}, indent=2, default=str)[:20000])
