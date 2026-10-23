"""24-hour (or any window) experiment report built only from recorded data.

Run from the command line:  python -m app.report
"""
from __future__ import annotations

import datetime as dt
import json

from .config import Settings
from .store import Store, now_ms


def _iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds") if ms else None


def start_experiment(store: Store, equity: float, hours: float = 24.0, clock=now_ms) -> dict:
    t = clock()
    exp = {"started_at": t, "planned_end_at": t + int(hours * 3_600_000), "hours": hours, "starting_equity": equity}
    store.set("experiment", exp)
    store.log("INFO", f"{hours:g}-hour paper experiment started at {_iso(t)} with equity {equity:.2f}", t)
    return exp


def build_report(store: Store, settings: Settings, clock=now_ms) -> dict:
    exp = store.get("experiment")
    if not exp:
        return {"error": "No experiment started. POST /api/experiment/start or use the dashboard button."}
    t0 = exp["started_at"]
    t1 = min(clock(), exp["planned_end_at"])
    w = (t0, t1)
    scans = store.one("SELECT COUNT(*) n, SUM(status='ok') ok, SUM(status='partial') partial, SUM(status='failed') failed, "
                      "COALESCE(SUM(api_requests),0) reqs FROM scans WHERE started_at BETWEEN ? AND ?", w)
    sigs = store.query("SELECT bias, COUNT(*) n FROM signals WHERE ts BETWEEN ? AND ? GROUP BY bias", w)
    opened = store.one("SELECT COUNT(*) n FROM positions WHERE opened_at BETWEEN ? AND ?", w)["n"]
    closed = store.query("SELECT * FROM positions WHERE status='CLOSED' AND closed_at BETWEEN ? AND ?", w)
    still_open = store.query("SELECT symbol, side, entry_price, mark_price, stop_loss, take_profit FROM positions "
                             "WHERE status='OPEN'")
    wins = [p for p in closed if p["net_pnl"] > 0]
    eq = store.query("SELECT ts, equity FROM equity WHERE ts BETWEEN ? AND ? ORDER BY ts", w)
    peak, mdd = exp["starting_equity"], 0.0
    for row in eq:
        peak = max(peak, row["equity"])
        mdd = max(mdd, (peak - row["equity"]) / peak if peak else 0)
    final_equity = eq[-1]["equity"] if eq else exp["starting_equity"]
    def ai_stats(provider):
        return store.one("SELECT COUNT(*) reviews, SUM(decision='APPROVE') approved, SUM(decision='REJECT') rejected, "
                         "SUM(model IS NOT NULL) api_calls, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) outp, "
                         "COALESCE(SUM(cost_usd),0) cost, SUM(error IS NOT NULL) errors FROM ai_decisions "
                         "WHERE ts BETWEEN ? AND ? AND provider=?", (*w, provider))

    def ai_block(a, model):
        return {"reviews": a["reviews"] or 0, "approved": a["approved"] or 0, "rejected": a["rejected"] or 0,
                "api_calls": a["api_calls"] or 0, "input_tokens": a["inp"], "output_tokens": a["outp"],
                "estimated_cost_usd": round(a["cost"], 5), "errors": a["errors"] or 0, "model": model}

    ai, gk = ai_stats("claude"), ai_stats("grok")
    errs = store.query("SELECT ts, level, message FROM events WHERE level IN ('WARN','ERROR') AND ts BETWEEN ? AND ? "
                       "ORDER BY ts DESC LIMIT 25", w)
    net = sum(p["net_pnl"] for p in closed)
    fees = sum(p["entry_fee"] + p["exit_fee"] for p in closed)
    funding = sum(p["funding_paid"] for p in closed)
    elapsed_h = (t1 - t0) / 3_600_000
    expected_scans = int(elapsed_h * 3600 / settings.scan_interval_seconds)
    return {
        "mode": "PAPER TRADING ONLY -- no real orders were placed",
        "window_utc": {"start": _iso(t0), "end": _iso(t1), "planned_end": _iso(exp["planned_end_at"]),
                       "elapsed_hours": round(elapsed_h, 2), "complete": clock() >= exp["planned_end_at"]},
        "scans": {"completed": scans["n"] or 0, "expected_approx": expected_scans, "ok": scans["ok"] or 0,
                  "partial": scans["partial"] or 0, "failed": scans["failed"] or 0,
                  "bitunix_api_requests": scans["reqs"]},
        "signals": {r["bias"]: r["n"] for r in sigs},
        "paper_trades": {"opened": opened, "closed": len(closed), "still_open": len(still_open),
                         "winning": len(wins), "losing": len(closed) - len(wins),
                         "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
                         "open_now": still_open},
        "pnl": {"net_realized_usdt": round(net, 4), "fees_usdt": round(fees, 4),
                "funding_usdt": round(funding, 4),
                "note": "net P&L already includes simulated fees, slippage and estimated funding"},
        "equity": {"starting": round(exp["starting_equity"], 4), "final": round(final_equity, 4),
                   "change_pct": round((final_equity / exp["starting_equity"] - 1) * 100, 3),
                   "max_drawdown_pct": round(mdd * 100, 3)},
        "claude": ai_block(ai, settings.claude_model),
        "grok": {**ai_block(gk, settings.grok_model), "mode": settings.grok_mode},
        "ai_total_cost_usd": round(ai["cost"] + gk["cost"], 5),
        "errors_and_warnings": [{"ts": _iso(e["ts"]), "level": e["level"], "message": e["message"][:400]} for e in errs],
        "limitations": [
            "News, token unlocks, exchange listings and open interest have no verified data source; they are reported as unavailable, not assumed neutral.",
            "Fills are simulated: taker fee + fixed slippage; real fills, partial fills, liquidation and queue position are not modelled.",
            "Funding is estimated from the current funding rate at each 8h settlement.",
            "Exits use 1-minute candles; when SL and TP are inside the same candle the stop is assumed to fill first.",
            "One 24-hour window is far too small a sample to judge a strategy; results are not evidence of future profit.",
        ],
    }


if __name__ == "__main__":
    from .config import load_dotenv
    load_dotenv()
    s = Settings.from_env()
    print(json.dumps(build_report(Store(s.db_path), s), indent=2))
