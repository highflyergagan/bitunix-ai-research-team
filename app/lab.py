"""Strategy Lab -- several strategies paper-trade side by side, each in its OWN $100 virtual account.

Every lab account uses the same risk engine as the main account (0.5% risk per trade including costs,
2% daily / 5% drawdown budgets, max 2 positions, fees + slippage, exits only from real later candles).
Lab strategies are rule-only (no AI calls), so they cost nothing to run and show whether a rule set has an
edge on its own. The main account (trend + AI Research Committee) is unaffected.

Strategies
  trend_rules      the main trend strategy WITHOUT the AI committee (measures what the AI adds or removes)
  mean_reversion   fade stretched moves: outside 2-sigma Bollinger band + RSI extreme, 1h trend not against
  breakout_volume  12h range breakout on expanding volume, 1h trend agreeing
  funding_extreme  contrarian when funding shows a crowded side, with 15m momentum turning
  pump_fade        short coins up >= 50% in 24h once 15m momentum breaks (wider slippage)
  pump_reversal    user's strategy: short coins up >= 150% in 24h once momentum breaks, trailing stop
"""
from __future__ import annotations

import dataclasses
import os
import statistics

from . import indicators as ind
from .bitunix import INTERVAL_MS
from .config import Settings
from .paper import PaperBroker
from .risk import RiskRejected
from .store import Store, now_ms
from .strategy import LONG, NO_TRADE, SHORT

STRATEGIES = {
    "trend_rules": "Trend (rules only, no AI)",
    "mean_reversion": "Mean reversion (Bollinger + RSI)",
    "breakout_volume": "Breakout with volume",
    "funding_extreme": "Funding-rate extreme (contrarian)",
    "pump_fade": "Pump fade: short after >=50% pump",
    "pump_reversal": "Pump Reversal: short >=150% pumps, trailing stop",
}


def _sig(symbol, bias, last, stop, tp, reasons, indicators=None):
    return {"symbol": symbol, "bias": bias, "entry": last, "stop_loss": stop, "take_profit": tp, "reasons": reasons,
            "blockers": [], "indicators": indicators or {}, "data_gaps": [], "score": len(reasons)}


def _levels(direction: str, last: float, atr: float, rr: float):
    d = max(1.5 * atr, 0.006 * last)
    if direction == LONG:
        return last - d, last + rr * d
    return last + d, last - rr * d


def _basic(m: dict, s: Settings):
    """Shared filters: enough candles, liquidity, sane volatility. Returns (closes15, last, atr) or None."""
    c15 = m.get("c15") or []
    if len(c15) < 60 or len(m.get("c1h") or []) < 60:
        return None
    if (m.get("quote_volume") or 0) < s.min_quote_volume:
        return None
    closes = [c["close"] for c in c15]
    last = closes[-1]
    a = ind.atr(c15)
    if not a or last <= 0 or not (0.0012 <= a / last <= 0.03):
        return None
    return closes, last, a


def mean_reversion(sym: str, m: dict, s: Settings):
    b = _basic(m, s)
    if not b:
        return None
    closes, last, a = b
    w = closes[-20:]
    mean, sd = statistics.mean(w), statistics.pstdev(w)
    r = ind.rsi(closes)
    c1 = [c["close"] for c in m["c1h"]]
    e20, e50 = ind.ema(c1, 20)[-1], ind.ema(c1, 50)[-1]
    if sd <= 0 or r is None:
        return None
    if last < mean - 2 * sd and r < 30 and e20 >= e50 * 0.995:
        stop, tp = _levels(LONG, last, a, 2.0)
        return _sig(sym, LONG, last, stop, tp, [f"below lower band, RSI {r:.0f}", "1h trend not down"])
    if last > mean + 2 * sd and r > 70 and e20 <= e50 * 1.005:
        stop, tp = _levels(SHORT, last, a, 2.0)
        return _sig(sym, SHORT, last, stop, tp, [f"above upper band, RSI {r:.0f}", "1h trend not up"])
    return None


def breakout_volume(sym: str, m: dict, s: Settings):
    b = _basic(m, s)
    if not b:
        return None
    closes, last, a = b
    c15 = m["c15"]
    prior = c15[-49:-1]
    hi, lo = max(c["high"] for c in prior), min(c["low"] for c in prior)
    vols = [c["quote_volume"] for c in c15]
    base = sum(vols[-40:-4]) / 36 if sum(vols[-40:-4]) > 0 else 0
    vr = (sum(vols[-4:]) / 4) / base if base else 0
    c1 = [c["close"] for c in m["c1h"]]
    e20, e50 = ind.ema(c1, 20)[-1], ind.ema(c1, 50)[-1]
    if last > hi and vr >= 1.5 and e20 >= e50:
        stop, tp = _levels(LONG, last, a, 2.5)
        return _sig(sym, LONG, last, stop, tp, [f"broke 12h high {hi:g}", f"volume {vr:.1f}x"])
    if last < lo and vr >= 1.5 and e20 <= e50:
        stop, tp = _levels(SHORT, last, a, 2.5)
        return _sig(sym, SHORT, last, stop, tp, [f"broke 12h low {lo:g}", f"volume {vr:.1f}x"])
    return None


def funding_extreme(sym: str, m: dict, s: Settings):
    fr = m.get("funding_rate")
    if fr is None:
        return None
    b = _basic(m, s)
    if not b:
        return None
    closes, last, a = b
    mm = ind.macd(closes)
    if not mm:
        return None
    if fr >= 0.0005 and mm["hist"] < 0 and mm["hist"] <= mm["hist_prev"]:
        stop, tp = _levels(SHORT, last, a, 2.0)
        return _sig(sym, SHORT, last, stop, tp, [f"funding {fr:.4%} crowded long", "15m momentum turning down"])
    if fr <= -0.0005 and mm["hist"] > 0 and mm["hist"] >= mm["hist_prev"]:
        stop, tp = _levels(LONG, last, a, 2.0)
        return _sig(sym, LONG, last, stop, tp, [f"funding {fr:.4%} crowded short", "15m momentum turning up"])
    return None


def pump_fade(sym: str, c15: list[dict], change_24h: float):
    """Short a coin that pumped >= 50% once its 15m momentum breaks. Stop above the recent spike high."""
    if len(c15) < 30:
        return None
    closes = [c["close"] for c in c15]
    last = closes[-1]
    e20 = ind.ema(closes, 20)[-1]
    recent_high = max(c["high"] for c in c15[-16:])  # last 4h
    if last >= e20 or recent_high < last * 1.05:
        return None
    stop = recent_high * 1.01
    if stop <= last or (stop - last) / last > 0.25:
        return None  # stop too wide to size sensibly
    tp = last - 2.0 * (stop - last)
    if tp <= 0:
        return None
    return _sig(sym, SHORT, last, stop, tp,
                [f"pumped {change_24h:+.0%} in 24h", "15m close below EMA20", f"{(recent_high / last - 1):.0%} off 4h high"])


def pump_reversal(sym: str, c15: list[dict], change_24h: float, trail_pct: float = 0.12):
    """User's strategy: coin up >= 150% in 24h; short once 15m momentum breaks and price is >= 8% off the 4h top.
    Stop 2% above the 4h top, then a trailing stop follows price down (only ever tightens). Far target (4R)."""
    if len(c15) < 30:
        return None
    closes = [c["close"] for c in c15]
    last = closes[-1]
    e20 = ind.ema(closes, 20)[-1]
    top = max(c["high"] for c in c15[-16:])
    if last >= e20 or last > top * 0.92:
        return None
    stop = top * 1.02
    if (stop - last) / last > 0.40:
        return None
    tp = last - 4.0 * (stop - last)
    if tp <= 0:
        tp = last * 0.05  # effectively "let the trailing stop decide"
    sig = _sig(sym, SHORT, last, stop, tp, [f"pumped {change_24h:+.0%} in 24h", "15m close below EMA20",
                                             f"{(1 - last / top):.0%} below 4h top", f"trailing stop {trail_pct:.0%}"])
    sig["trail_pct"] = trail_pct
    return sig


RULE_FUNCS = {"mean_reversion": mean_reversion, "breakout_volume": breakout_volume, "funding_extreme": funding_extreme}


class StrategyLab:
    def __init__(self, settings: Settings, client, candles_since, clock=now_ms, pump_threshold: float = 0.50,
                 max_pump_checks: int = 5):
        self.s = settings
        self.client = client
        self.candles_since = candles_since
        self.clock = clock
        self.pump_threshold = pump_threshold
        self.max_pump_checks = max_pump_checks
        self.brokers: dict[str, PaperBroker] = {}
        for key in STRATEGIES:
            st = Store(os.path.join(settings.data_dir, f"lab_{key}.db"))
            cfg = settings
            if key in ("pump_fade", "pump_reversal"):
                # pumped coins are thin: assume much worse fills. Wide stops at 0.5% risk give small positions,
                # so allow paper positions down to $1 notional.
                cfg = dataclasses.replace(settings, slippage=0.003, alt_extra_slippage=0.002, min_notional=1.0)
            self.brokers[key] = PaperBroker(st, cfg, clock)

    # ---------- per scan ----------
    def run(self, market: dict[str, dict], prices: dict[str, float], tickers: list[dict]) -> dict:
        out = {}
        for key, br in self.brokers.items():
            res = {"opened": 0, "closed": 0, "errors": []}
            try:
                self._manage_exits(br, prices, market, res)
                br.roll_day_and_limits()
                for sig in self._signals(key, market, tickers):
                    try:
                        br.open_position(sig, market_price=prices.get(sig["symbol"]) or sig["entry"])
                        res["opened"] += 1
                    except RiskRejected:
                        pass
                br.roll_day_and_limits()
                br.record_equity()
            except Exception as exc:  # one strategy failing must not affect others or the main scan
                res["errors"].append(str(exc)[:200])
                br.store.log("ERROR", f"lab {key}: {exc}")
            out[key] = res
        return out

    def _manage_exits(self, br: PaperBroker, prices, market, res):
        for p in br.open_positions():
            try:
                cs = self.candles_since(p["symbol"], p["last_checked_ms"])
                fr = (market.get(p["symbol"]) or {}).get("funding_rate")
                after = br.process_candles(p, cs, INTERVAL_MS["1m"], fr)
                if after["status"] == "CLOSED":
                    res["closed"] += 1
                elif prices.get(p["symbol"]):
                    br.store.execute("UPDATE positions SET mark_price=? WHERE id=?", (prices[p["symbol"]], p["id"]))
            except Exception as exc:
                res["errors"].append(f"{p['symbol']} exit: {exc}"[:200])

    def _signals(self, key: str, market: dict, tickers: list[dict]):
        sigs = []
        if key == "trend_rules":
            for sym, m in market.items():
                sig = m.get("main_signal")
                if sig and sig["bias"] in (LONG, SHORT):
                    sigs.append(sig)
        elif key in RULE_FUNCS:
            for sym, m in market.items():
                sig = RULE_FUNCS[key](sym, m, self.s)
                if sig:
                    sigs.append(sig)
        elif key in ("pump_fade", "pump_reversal"):
            threshold = self.pump_threshold if key == "pump_fade" else 1.50
            movers = []
            for x in tickers:
                try:
                    o, last, vol = float(x.get("open") or 0), float(x.get("lastPrice") or 0), float(x.get("quoteVol") or 0)
                except (TypeError, ValueError):
                    continue
                if o > 0 and last > 0 and vol >= 1_000_000 and last / o - 1 >= threshold:
                    movers.append((last / o - 1, x["symbol"]))
            for chg, sym in sorted(movers, reverse=True)[: self.max_pump_checks]:
                try:
                    now = self.clock()
                    c15 = [c for c in self.client.klines(sym, "15m", 100) if c["time"] + INTERVAL_MS["15m"] <= now]
                    sig = pump_fade(sym, c15, chg) if key == "pump_fade" else pump_reversal(sym, c15, chg)
                    if sig:
                        sigs.append(sig)
                except Exception:
                    continue
        sigs.sort(key=lambda g: g.get("score", 0), reverse=True)
        return sigs

    # ---------- reporting ----------
    def summary(self) -> list[dict]:
        rows = []
        for key, br in self.brokers.items():
            a = br.account()
            eq = br.store.query("SELECT equity FROM equity ORDER BY ts")
            peak, mdd = br.store.get("starting_balance"), 0.0
            for r in eq:
                peak = max(peak, r["equity"])
                mdd = max(mdd, (peak - r["equity"]) / peak if peak else 0)
            closed = br.store.query("SELECT net_pnl, entry_fee, exit_fee FROM positions WHERE status='CLOSED'")
            rows.append({
                "key": key, "name": STRATEGIES[key], "ai": False, "since": br.store.get("account_created_at"),
                "equity": a["equity"], "return_pct": a["total_return_pct"], "open": a["open_positions"],
                "closed": a["closed_trades"], "wins": a["wins"], "losses": a["losses"], "win_rate_pct": a["win_rate_pct"],
                "fees_usdt": round(sum(c["entry_fee"] + c["exit_fee"] for c in closed), 4),
                "max_drawdown_pct": round(mdd * 100, 3), "halted": a["halted"],
                "open_positions": [{k: p[k] for k in ("symbol", "side", "entry_price", "mark_price", "stop_loss", "take_profit", "opened_at")}
                                   for p in br.open_positions()],
            })
        return rows
