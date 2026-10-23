"""Deterministic signal engine: LONG BIAS / SHORT BIAS / NO TRADE.

Signals are research hypotheses, not predictions. Every LONG/SHORT signal carries
a concrete stop-loss and take-profit derived from ATR and market structure.
"""
from __future__ import annotations

from . import indicators as ind

LONG, SHORT, NO_TRADE = "LONG BIAS", "SHORT BIAS", "NO TRADE"
MIN_CANDLES = 60
UNAVAILABLE_FEEDS = ["news", "token_unlocks", "exchange_listings", "open_interest"]


def _r(x, n=6):
    return None if x is None else round(x, n)


def generate_signal(symbol: str, c15: list[dict], c1h: list[dict], *, quote_volume_24h: float | None,
                    funding_rate: float | None, min_quote_volume: float,
                    require_verified_event_feeds: bool = False) -> dict:
    sig = {
        "symbol": symbol,
        "bias": NO_TRADE,
        "score": 0,
        "entry": None,
        "stop_loss": None,
        "take_profit": None,
        "reasons": [],
        "blockers": [],
        "indicators": {},
        "data_gaps": list(UNAVAILABLE_FEEDS),
    }
    if len(c15) < MIN_CANDLES or len(c1h) < MIN_CANDLES:
        sig["blockers"].append(f"insufficient candles (15m={len(c15)}, 1h={len(c1h)}, need {MIN_CANDLES})")
        return sig

    closes15 = [c["close"] for c in c15]
    closes1h = [c["close"] for c in c1h]
    last = closes15[-1]
    if last <= 0:
        sig["blockers"].append("invalid price")
        return sig

    e20_15, e50_15 = ind.ema(closes15, 20)[-1], ind.ema(closes15, 50)[-1]
    e20_1h, e50_1h = ind.ema(closes1h, 20)[-1], ind.ema(closes1h, 50)[-1]
    rsi15 = ind.rsi(closes15)
    m = ind.macd(closes15)
    a = ind.atr(c15)
    lv = ind.swing_levels(c15)
    vols = [c["quote_volume"] for c in c15]
    vol_ratio = (sum(vols[-4:]) / 4) / (sum(vols[-40:-4]) / 36) if sum(vols[-40:-4]) > 0 else None
    atr_pct = a / last if a else None
    mom = last / closes15[-9] - 1  # ~2h momentum

    sig["indicators"] = {
        "price": _r(last), "ema20_15m": _r(e20_15), "ema50_15m": _r(e50_15),
        "ema20_1h": _r(e20_1h), "ema50_1h": _r(e50_1h), "rsi_15m": _r(rsi15, 1),
        "macd_hist_15m": _r(m["hist"] if m else None, 8), "macd_hist_prev_15m": _r(m["hist_prev"] if m else None, 8),
        "atr_15m": _r(a), "atr_pct_15m": _r(atr_pct, 5), "volume_ratio_1h_vs_9h": _r(vol_ratio, 2),
        "momentum_2h": _r(mom, 5), "support": _r(lv["support"]), "resistance": _r(lv["resistance"]),
        "range_high_12h": _r(lv["range_high"]), "range_low_12h": _r(lv["range_low"]),
        "funding_rate": funding_rate, "quote_volume_24h": quote_volume_24h,
    }
    if funding_rate is None:
        sig["data_gaps"].append("funding_rate")

    # --- hard filters ---
    if quote_volume_24h is None or quote_volume_24h < min_quote_volume:
        sig["blockers"].append(f"liquidity below {min_quote_volume:,.0f} USDT 24h volume")
    if not a or not m or rsi15 is None:
        sig["blockers"].append("indicator calculation failed")
    elif not (0.0012 <= atr_pct <= 0.03):
        sig["blockers"].append(f"volatility out of range (ATR {atr_pct:.3%} of price)")
    if require_verified_event_feeds:
        sig["blockers"].append("verified news/unlock/listing feeds required but unavailable")

    # --- directional score ---
    long_pts, short_pts = [], []
    if last > e50_1h and e20_1h > e50_1h:
        long_pts.append("1h uptrend (price > EMA50, EMA20 > EMA50)")
    if last < e50_1h and e20_1h < e50_1h:
        short_pts.append("1h downtrend (price < EMA50, EMA20 < EMA50)")
    if e20_15 > e50_15 and last > e20_15:
        long_pts.append("15m structure bullish")
    if e20_15 < e50_15 and last < e20_15:
        short_pts.append("15m structure bearish")
    if m and m["hist"] > 0 and m["hist"] >= m["hist_prev"]:
        long_pts.append("MACD histogram positive and rising")
    if m and m["hist"] < 0 and m["hist"] <= m["hist_prev"]:
        short_pts.append("MACD histogram negative and falling")
    if rsi15 is not None and 50 <= rsi15 <= 68:
        long_pts.append(f"RSI {rsi15:.0f} bullish but not overbought")
    if rsi15 is not None and 32 <= rsi15 <= 50:
        short_pts.append(f"RSI {rsi15:.0f} bearish but not oversold")
    if vol_ratio is not None and vol_ratio >= 1.0:
        (long_pts if mom > 0 else short_pts).append(f"volume expanding ({vol_ratio:.2f}x) with momentum")

    direction = None
    if len(long_pts) >= 4 and len(short_pts) == 0:
        direction, pts = LONG, long_pts
    elif len(short_pts) >= 4 and len(long_pts) == 0:
        direction, pts = SHORT, short_pts
    else:
        sig["reasons"].append(f"no confluence (long {len(long_pts)}/5, short {len(short_pts)}/5)")
        sig["reasons"] += long_pts + short_pts
        return sig
    htf_ok = (e20_1h > e50_1h and last > e50_1h) if direction == LONG else (e20_1h < e50_1h and last < e50_1h)
    if not htf_ok:
        sig["reasons"].append("higher-timeframe trend does not agree")
        sig["reasons"] += pts
        return sig

    # --- overextension filter ---
    if direction == LONG and rsi15 is not None and rsi15 > 72:
        sig["blockers"].append(f"RSI {rsi15:.0f} overbought -- late entry")
    if direction == SHORT and rsi15 is not None and rsi15 < 28:
        sig["blockers"].append(f"RSI {rsi15:.0f} oversold -- late entry")

    # --- funding filter ---
    if funding_rate is not None:
        if direction == LONG and funding_rate > 0.0005:
            sig["blockers"].append(f"funding {funding_rate:.4%} crowded long")
        if direction == SHORT and funding_rate < -0.0005:
            sig["blockers"].append(f"funding {funding_rate:.4%} crowded short")

    # --- levels ---
    # min 0.6% stop so fees/slippage don't dominate; target 2.5R
    stop_dist = max(1.5 * a, 0.006 * last)
    if direction == LONG:
        stop = last - stop_dist
        if lv["support"] and last - stop_dist - a < lv["support"] < last - 0.5 * stop_dist:
            stop = min(stop, lv["support"] - 0.25 * a)
        tp = last + 2.5 * (last - stop)
        if lv["resistance"] and lv["resistance"] < last + 0.5 * (tp - last):
            sig["blockers"].append("resistance too close to entry")
    else:
        stop = last + stop_dist
        if lv["resistance"] and last + 0.5 * stop_dist < lv["resistance"] < last + stop_dist + a:
            stop = max(stop, lv["resistance"] + 0.25 * a)
        tp = last - 2.5 * (stop - last)
        if lv["support"] and lv["support"] > last - 0.5 * (last - tp):
            sig["blockers"].append("support too close to entry")
    if tp <= 0 or stop <= 0:
        sig["blockers"].append("invalid levels")

    sig["reasons"] = pts
    sig["score"] = len(pts)
    sig["entry"], sig["stop_loss"], sig["take_profit"] = _r(last), _r(stop), _r(tp)
    if not sig["blockers"]:
        sig["bias"] = direction
    return sig
