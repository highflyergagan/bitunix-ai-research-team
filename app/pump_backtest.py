"""Walk-forward backtest for the Pump Exhaustion SHORT agent vs the existing reversal strategies.

Data   : Bitunix PUBLIC klines only (read-only). 4h candles screen every listed USDT perpetual for 24h pumps over the
         last N days; 15m candles are then fetched around each pump (2 days before to 3 days after) and cached.
Rules  : exactly the live functions -- pump_agent.evaluate, lab.pump_fade, lab.pump_reversal -- on CLOSED 15m candles.
Fills  : entry at the NEXT 15m candle's open; 0.5% adverse slippage on every fill, 0.06% taker fee per side;
         stop checked before target in the same candle; gaps fill at the open; 24h time stop; trailing stop for
         pump_reversal exactly as the paper broker; funding: shorts assumed to PAY 0.05% per 8h settlement
         (historical funding is not available from the public API, so a conservative constant is used).
Results: in R (1R = planned loss at the stop incl. costs). Equity compounds 0.5% risk per trade.
Validation (gate for automatic paper execution of the agent at its configured threshold):
         out-of-sample (last 40% of the period) >= 20 trades, expectancy >= +0.10R, profit factor >= 1.3,
         max drawdown <= 5%, AND anchored walk-forward out-of-sample expectancy > 0 with >= 20 trades.
Known biases: coins delisted before today are not in the ticker list (survivorship); no order book history, so
spread/depth gates are not applied in the backtest; portfolio limits (max 2 open) are not simulated.

Run on a machine that can reach Bitunix:  python -m app.pump_backtest
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import traceback

from .bitunix import INTERVAL_MS, BitunixPublicClient
from .config import Settings, load_dotenv
from .lab import pump_fade, pump_reversal
from .pump_agent import evaluate
from .store import Store, now_ms

THRESHOLDS = [0.50, 0.75, 1.00, 1.50, 2.00]
STRATEGIES = {
    "pump_exhaustion": "Pump Exhaustion agent (breakdown + failed retest)",
    "pump_fade": "Pump fade (existing lab rule)",
    "pump_reversal": "Pump Reversal (existing lab rule, 12% trailing)",
}
SLIP, FEE, FUNDING_8H = 0.005, 0.0006, 0.0005
RISK = 0.005
M15, H4, DAY = INTERVAL_MS["15m"], INTERVAL_MS["4h"], 86_400_000
_lock = threading.Lock()


# ---------------------------------------------------------------- data
def fetch_range(client, sym: str, interval: str, start: int, end: int, cache: Store | None = None) -> list[dict]:
    key = f"{sym}|{interval}|{start}|{end}"
    if cache is not None:
        hit = cache.one("SELECT value FROM bt_cache WHERE key=?", (key,))
        if hit:
            return json.loads(hit["value"])
    out: dict[int, dict] = {}
    e, prev_first = end, None
    for _ in range(80):
        b = [c for c in client.klines(sym, interval, 200, end_ms=e) if c["time"] < e]
        if not b:
            break
        first = b[0]["time"]
        if prev_first is not None and first >= prev_first:
            break  # endTime not honoured -> stop
        prev_first = first
        for c in b:
            out[c["time"]] = c
        if first <= start:
            break
        e = first
    step = INTERVAL_MS[interval]
    rows = [out[k] for k in sorted(out) if start <= k and k + step <= end]
    if cache is not None:
        cache.execute("INSERT OR REPLACE INTO bt_cache(key,value) VALUES(?,?)", (key, json.dumps(rows)))
    return rows


def pump_ranges(c4h: list[dict], screen: float = 0.45) -> list[tuple[int, int]]:
    """15m fetch windows (2 days before to 3 days after) around every 24h pump >= screen, merged when overlapping."""
    hits = [c4h[j]["time"] for j in range(5, len(c4h)) if c4h[j - 5]["open"] > 0 and
            c4h[j]["close"] / c4h[j - 5]["open"] - 1 >= screen]
    ranges: list[list[int]] = []
    for t in hits:
        a, b = t - 2 * DAY, t + 3 * DAY
        if ranges and a <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    return [(a, b) for a, b in ranges]


# ---------------------------------------------------------------- simulation
def strategy_signals(sym: str, c15: list[dict], min_change: float = THRESHOLDS[0]) -> dict[str, list]:
    out = {k: [] for k in STRATEGIES}
    for i in range(110, len(c15) - 1):
        chg = c15[i]["close"] / c15[i - 96]["close"] - 1
        if chg < min_change:
            continue
        win = c15[max(0, i - 199): i + 1]
        vol24 = sum(c["quote_volume"] for c in c15[i - 95: i + 1])
        ex = evaluate(sym, win, change_24h=chg, threshold=min_change, quote_volume=vol24, live_checks=False)
        if ex["bias"] == "SHORT BIAS":
            out["pump_exhaustion"].append((i, chg, ex))
        if vol24 >= 1_000_000:  # the lab pump rules require >= $1M 24h volume
            f = pump_fade(sym, win[-100:], chg)
            if f:
                out["pump_fade"].append((i, chg, f))
            r = pump_reversal(sym, win[-100:], chg)
            if r:
                out["pump_reversal"].append((i, chg, r))
    return out


def _settlements(a: int, b: int) -> int:
    return max(0, b // 28_800_000 - a // 28_800_000)


def simulate(sym: str, c15: list[dict], sigs: list, threshold: float) -> list[dict]:
    trades, busy_until = [], -1
    for i, chg, sig in sigs:
        if chg < threshold or i <= busy_until:
            continue
        e = i + 1
        if e + 1 >= len(c15):
            continue
        ep, stop, tp = c15[e]["open"], sig["stop_loss"], sig["take_profit"]
        if not (tp < ep < stop):
            continue  # price already beyond a level at the next open -> no entry
        fe = ep * (1 - SLIP)
        sf = stop * (1 + SLIP)
        risk_unit = sf - fe + FEE * (fe + sf)
        trail = sig.get("trail_pct")
        px = reason = None
        j = e
        last_j = min(e + 95, len(c15) - 1)
        for j in range(e, last_j + 1):
            c = c15[j]
            if c["high"] >= stop:
                gap = c["open"] >= stop
                px = c["open"] if gap else stop
                reason = ("trailing stop" if trail and stop < ep else "stop") + (" (gap)" if gap else "")
                break
            if c["low"] <= tp:
                px = c["open"] if c["open"] <= tp else tp
                reason = "target"
                break
            if trail and c["low"] <= ep * (1 - trail):
                stop = min(stop, c["low"] * (1 + trail))
        if px is None:
            if last_j < e + 95:
                continue  # data ends before the trade could finish -> excluded, not guessed
            px, reason = c15[last_j]["close"], "24h time stop"
        xf = px * (1 + SLIP)
        exit_t = c15[j]["time"] + M15
        funding = FUNDING_8H * ep * _settlements(c15[e]["time"], exit_t)
        pnl = fe - xf - FEE * (fe + xf) - funding
        trades.append({"symbol": sym, "entry_time": c15[e]["time"], "exit_time": exit_t, "change_24h": round(chg, 3),
                       "r": round(pnl / risk_unit, 4), "reason": reason,
                       "net_move_pct": round(pnl / fe * 100, 3)})
        busy_until = j + 4  # 60-minute cooldown after the exit, as in the live risk engine
    return trades


def metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate_pct": None, "expectancy_r": None, "profit_factor": None,
                "total_return_pct": 0.0, "max_drawdown_pct": 0.0}
    rs = [t["r"] for t in sorted(trades, key=lambda t: t["exit_time"])]
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= 1 + RISK * r
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    return {"trades": n, "win_rate_pct": round(sum(r > 0 for r in rs) / n * 100, 1),
            "expectancy_r": round(statistics.mean(rs), 3), "avg_win_r": round(gw / max(1, sum(r > 0 for r in rs)), 3),
            "avg_loss_r": round(-gl / max(1, sum(r < 0 for r in rs)), 3),
            "profit_factor": round(gw / gl, 2) if gl else (None if not gw else 99.0),
            "total_return_pct": round((eq - 1) * 100, 2), "max_drawdown_pct": round(mdd * 100, 2)}


def analyse(all_trades: dict, start: int, end: int, agent_threshold: float) -> dict:
    """all_trades[strategy][threshold] -> list of trades. In-sample = first 60% of time, out-of-sample = last 40%;
    plus an anchored walk-forward (4 segments; threshold chosen on past data, tested on the next segment)."""
    split = start + int((end - start) * 0.6)
    table = []
    for sk in STRATEGIES:
        for T in THRESHOLDS:
            tr = all_trades[sk][T]
            table.append({"strategy": sk, "name": STRATEGIES[sk], "threshold_pct": int(T * 100),
                          "all": metrics(tr), "in_sample": metrics([t for t in tr if t["entry_time"] < split]),
                          "out_of_sample": metrics([t for t in tr if t["entry_time"] >= split])})
    seg = (end - start) / 4
    wf = {}
    for sk in STRATEGIES:
        oos, chosen = [], []
        for k in (1, 2, 3):
            a, b = start + int(seg * k), start + int(seg * (k + 1))
            best, best_e = agent_threshold, None
            for T in THRESHOLDS:
                m = metrics([t for t in all_trades[sk][T] if t["entry_time"] < a])
                if m["trades"] >= 5 and (best_e is None or m["expectancy_r"] > best_e):
                    best, best_e = T, m["expectancy_r"]
            chosen.append(int(best * 100))
            oos += [t for t in all_trades[sk][best] if a <= t["entry_time"] < b]
        wf[sk] = {"thresholds_chosen_pct": chosen, "out_of_sample": metrics(oos)}
    return {"split_time": split, "table": table, "walk_forward": wf}


def validate(analysis: dict, agent_threshold: float) -> dict:
    row = next(r for r in analysis["table"] if r["strategy"] == "pump_exhaustion"
               and r["threshold_pct"] == int(round(agent_threshold * 100)))
    o, w = row["out_of_sample"], analysis["walk_forward"]["pump_exhaustion"]["out_of_sample"]
    checks = {
        "oos_trades>=20": o["trades"] >= 20,
        "oos_expectancy>=0.10R": (o["expectancy_r"] or -9) >= 0.10,
        "oos_profit_factor>=1.3": (o["profit_factor"] or 0) >= 1.3,
        "oos_max_drawdown<=5%": o["max_drawdown_pct"] <= 5.0 and o["trades"] > 0,
        "walk_forward_trades>=20": w["trades"] >= 20,
        "walk_forward_expectancy>0": (w["expectancy_r"] or -9) > 0,
    }
    ok = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    return {"validated": ok, "threshold_pct": int(round(agent_threshold * 100)), "checks": checks,
            "reason": "passed all out-of-sample and walk-forward checks" if ok else "failed: " + ", ".join(failed),
            "summary": {"out_of_sample": o, "walk_forward": w}}


# ---------------------------------------------------------------- runner
def run_backtest(settings: Settings, agent_store: Store, client=None, clock=now_ms, symbols: list[str] | None = None) -> dict:
    if not _lock.acquire(blocking=False):
        return {"state": "already running"}
    status = {"state": "running", "phase": "listing symbols", "done": 0, "total": 0, "started_at": clock(),
              "days": settings.pump_backtest_days}
    try:
        agent_store.set("backtest_status", status)
        client = client or BitunixPublicClient(pause=max(settings.request_pause_seconds, 0.15))
        cache = Store(os.path.join(settings.data_dir, "pump_backtest_cache.db"))
        cache.conn.execute("CREATE TABLE IF NOT EXISTS bt_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        end = (clock() // H4) * H4
        start = end - settings.pump_backtest_days * DAY
        if symbols is None:
            symbols = sorted({x["symbol"] for x in client.tickers() if str(x.get("symbol", "")).endswith("USDT")})
        status.update(total=len(symbols), phase="scanning 4h history for pumps")
        all_trades = {sk: {T: [] for T in THRESHOLDS} for sk in STRATEGIES}
        events = signals_n = 0
        for n, sym in enumerate(symbols, 1):
            try:
                c4 = fetch_range(client, sym, "4h", start, end, cache)
                for a, b in pump_ranges(c4):
                    events += 1
                    c15 = fetch_range(client, sym, "15m", max(a, start - 2 * DAY), min(b, end), cache)
                    sigs = strategy_signals(sym, c15)
                    signals_n += sum(len(v) for v in sigs.values())
                    for sk in STRATEGIES:
                        for T in THRESHOLDS:
                            all_trades[sk][T] += simulate(sym, c15, sigs[sk], T)
            except Exception as exc:
                status.setdefault("symbol_errors", []).append(f"{sym}: {str(exc)[:120]}")
                status["symbol_errors"] = status["symbol_errors"][-20:]
            status.update(done=n, pump_windows=events)
            if n % 10 == 0:
                agent_store.set("backtest_status", status)
        an = analyse(all_trades, start, end, settings.pump_agent_threshold)
        val = validate(an, settings.pump_agent_threshold)
        result = {"period": {"start": start, "end": end, "days": settings.pump_backtest_days},
                  "symbols": len(symbols), "pump_windows": events, "raw_signals": signals_n,
                  "costs": {"slippage_per_fill": SLIP, "taker_fee_per_side": FEE,
                            "funding_shorts_pay_per_8h": FUNDING_8H, "risk_per_trade": RISK},
                  "biases": ["survivorship: delisted coins are not in today's ticker list",
                             "no historical order book: spread/depth gates not applied",
                             "historical funding unavailable: constant 0.05%/8h paid by shorts assumed",
                             "max-2-open-positions portfolio limit not simulated",
                             "15m candles: intrabar order unknown -> stop assumed first"],
                  **an, "validation": val,
                  "sample_trades": sorted(all_trades["pump_exhaustion"][settings.pump_agent_threshold],
                                          key=lambda t: t["entry_time"])[-30:]}
        agent_store.set("backtest_result", result)
        agent_store.set("validation", {**val, "validated_at": clock()})
        status.update(state="done", phase="finished", finished_at=clock())
        agent_store.set("backtest_status", status)
        return result
    except Exception as exc:
        status.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:400],
                      trace=traceback.format_exc()[-800:], finished_at=clock())
        agent_store.set("backtest_status", status)
        return status
    finally:
        _lock.release()


def start_background(settings: Settings, agent_store: Store) -> dict:
    if _lock.locked():
        return {"started": False, "reason": "a backtest is already running", "status": agent_store.get("backtest_status")}
    threading.Thread(target=run_backtest, args=(settings, agent_store), name="pump-backtest", daemon=True).start()
    return {"started": True}


if __name__ == "__main__":
    load_dotenv()
    s = Settings.from_env()
    st = Store(os.path.join(s.data_dir, "agent_pump_exhaustion.db"))
    r = run_backtest(s, st)
    print(json.dumps({k: r.get(k) for k in ("symbols", "pump_windows", "walk_forward", "validation")}, indent=2))
