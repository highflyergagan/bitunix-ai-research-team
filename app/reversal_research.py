"""Research the user's own reversal style ("Gagan reversal"), as traded on JCT and CAP. RESEARCH ONLY.

Rules (one function, tested over a grid of settings):
  1. coin up >= min_pump over the rolling 24h (15m candles, 96 bars)
  2. the 24h top is behind us and price is >= min_off_top below it
  3. a LOWER HIGH has formed after the top (a swing high below the top, confirmed by 2 candles each side)
  4. trigger: the last closed 15m candle closes below EMA20 AND below the low of the lower-high candle
  5. stop = lower-high + stop_buffer; take-profit far (8R) -- exits mostly by the trailing stop, the stop or time
  6. trailing stop: once price is trail_pct in profit, the stop follows price down (only tightens)
Every result is net of fees, slippage (tested at several levels) and funding paid by shorts. Validation is an
anchored walk-forward: settings chosen on past data only, scored on the next unseen period.

Data: the 15m pump windows already cached by the pump backtest (data/pump_backtest_cache.db), so no API calls.
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import statistics

from . import indicators as ind
from .pump_backtest import metrics, simulate

GRID = {
    "min_pump": [0.30, 0.50, 1.00, 1.50],
    "min_off_top": [0.05, 0.10],
    "stop_buffer": [0.005, 0.02],
    "trail_pct": [0.06, 0.10, 0.15],
}
SLIPPAGES = [0.002, 0.003, 0.005]
MAX_HOLD_BARS = 192  # 48h


def pivots_high(c: list[dict], wing: int = 2) -> list[int]:
    return [i for i in range(wing, len(c) - wing) if c[i]["high"] >= max(x["high"] for x in c[i - wing:i + wing + 1])]


def user_reversal(c15: list[dict], chg: float, p: dict) -> dict | None:
    """c15 = closed candles up to now (>= 120). Returns a SHORT signal dict or None."""
    n = len(c15)
    if n < 120 or chg < p["min_pump"]:
        return None
    w0 = n - 96
    top_i = max(range(w0, n), key=lambda i: c15[i]["high"])
    top = c15[top_i]["high"]
    last = c15[-1]["close"]
    if last > top * (1 - p["min_off_top"]) or top_i > n - 6:
        return None
    lh = [i for i in pivots_high(c15[top_i:], 2) if i > 0]
    lh = [top_i + i for i in lh if c15[top_i + i]["high"] < top * 0.99 and top_i + i <= n - 3]
    if not lh:
        return None
    li = lh[-1]
    if n - 1 - li > 24:  # lower high must be recent (within 6h)
        return None
    e20 = ind.ema([x["close"] for x in c15], 20)[-1]
    if not (last < e20 and last < c15[li]["low"] and c15[-2]["close"] >= min(c15[li]["low"], e20)):
        return None  # trigger only on the FIRST close below (no chasing)
    stop = c15[li]["high"] * (1 + p["stop_buffer"])
    if (stop - last) / last > 0.30:
        return None
    tp = max(last - 8 * (stop - last), last * 0.02)
    return {"bias": "SHORT BIAS", "entry": last, "stop_loss": stop, "take_profit": tp, "trail_pct": p["trail_pct"]}


def load_windows(cache_path: str) -> list[tuple[str, list[dict]]]:
    con = sqlite3.connect(cache_path)
    out = []
    for key, val in con.execute("SELECT key, value FROM bt_cache WHERE key LIKE '%|15m|%'"):
        rows = json.loads(val)
        if 150 <= len(rows) <= 15 * 96:  # pump windows only (the comparison run's 120-day series are skipped)
            out.append((key.split("|")[0], rows))
    con.close()
    return out


def run(cache_path: str) -> dict:
    windows = load_windows(cache_path)
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    # signals per combo (independent of slippage)
    sigs = {i: [] for i in range(len(combos))}
    for wi, (sym, c) in enumerate(windows):
        for k in range(120, len(c) - 1):
            chg = c[k]["close"] / c[k - 96]["close"] - 1
            if chg < min(GRID["min_pump"]):
                continue
            win = c[max(0, k - 199):k + 1]
            for ci, p in enumerate(combos):
                s = user_reversal(win, chg, p)
                if s:
                    sigs[ci].append((wi, k, chg, s))
    t_all = [x["time"] for _, c in windows for x in c]
    start, end = min(t_all), max(t_all)
    results = {}
    for slip in SLIPPAGES:
        per_combo = []
        for ci, p in enumerate(combos):
            trades = []
            by_win: dict[int, list] = {}
            for wi, k, chg, s in sigs[ci]:
                by_win.setdefault(wi, []).append((k, chg, s))
            for wi, lst in by_win.items():
                sym, c = windows[wi]
                trades += simulate(sym, c, lst, 0.0, slip=slip, max_bars=MAX_HOLD_BARS)
            per_combo.append((p, trades))
        # anchored walk-forward over 4 segments
        seg = (end - start) / 4
        oos, picks = [], []
        for k in (1, 2, 3):
            a, b = start + seg * k, start + seg * (k + 1)
            best, best_e = None, None
            for p, tr in per_combo:
                m = metrics([t for t in tr if t["entry_time"] < a])
                if m["trades"] >= 10 and (best_e is None or m["expectancy_r"] > best_e):
                    best, best_e = (p, tr), m["expectancy_r"]
            if best:
                picks.append(best[0])
                oos += [t for t in best[1] if a <= t["entry_time"] < b]
        table = sorted(({"params": p, **metrics(tr)} for p, tr in per_combo),
                       key=lambda r: (r["expectancy_r"] if r["expectancy_r"] is not None else -9), reverse=True)
        results[f"slippage_{slip:.3f}"] = {"walk_forward_oos": metrics(oos), "picked_params": picks,
                                          "best_in_sample_combos": table[:5],
                                          "share_of_combos_profitable": round(sum(1 for r in table if (r["expectancy_r"] or -1) > 0) / len(table), 2)}
    return {"windows": len(windows), "period": {"start": start, "end": end}, "combos": len(combos),
            "rules": __doc__.split("Rules")[1].split("Data:")[0].strip(), "results": results}


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "pump_backtest_cache.db")), indent=1)[:20000])
