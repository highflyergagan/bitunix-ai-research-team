"""Deterministic risk controls. These run AFTER any AI review and cannot be overridden by it."""
from __future__ import annotations

from dataclasses import dataclass

from .config import LIVE_TRADING_ENABLED, Settings


class RiskRejected(Exception):
    pass


def slippage_for(symbol: str, s: Settings) -> float:
    return s.slippage if symbol in ("BTCUSDT", "ETHUSDT") else s.slippage + s.alt_extra_slippage


def fill_price(price: float, side: str, opening: bool, slip: float) -> float:
    """Adverse fill: buying fills higher, selling fills lower."""
    buying = (side == "LONG") == opening
    return price * (1 + slip) if buying else price * (1 - slip)


def validate_levels(side: str, entry: float, stop: float | None, tp: float | None) -> None:
    if not entry or entry <= 0:
        raise RiskRejected("invalid entry price")
    if stop is None or tp is None or stop <= 0 or tp <= 0:
        raise RiskRejected("trade requires a valid stop-loss and take-profit")
    if side == "LONG" and not (stop < entry < tp):
        raise RiskRejected("LONG requires stop < entry < take-profit")
    if side == "SHORT" and not (tp < entry < stop):
        raise RiskRejected("SHORT requires take-profit < entry < stop")


@dataclass
class Sizing:
    qty: float
    fill_entry: float
    notional: float
    risk_usd: float
    loss_per_unit: float
    reward_per_unit: float
    reward_risk: float


def size_position(symbol: str, side: str, entry: float, stop: float, tp: float, equity: float,
                  available_notional: float, s: Settings) -> Sizing:
    validate_levels(side, entry, stop, tp)
    slip = slippage_for(symbol, s)
    fe = fill_price(entry, side, True, slip)
    stop_fill = fill_price(stop, side, False, slip)
    tp_fill = fill_price(tp, side, False, slip)
    fee = s.taker_fee
    loss_per_unit = abs(fe - stop_fill) + fe * fee + stop_fill * fee
    reward_per_unit = abs(tp_fill - fe) - fe * fee - tp_fill * fee
    if loss_per_unit <= 0:
        raise RiskRejected("invalid stop distance")
    rr = reward_per_unit / loss_per_unit
    if rr < s.min_reward_risk:
        raise RiskRejected(f"reward:risk {rr:.2f} after fees/slippage below {s.min_reward_risk}")
    risk_usd = equity * s.risk_per_trade
    qty = risk_usd / loss_per_unit
    max_qty = max(available_notional, 0) / fe
    if qty > max_qty:
        qty = max_qty  # leverage cap reduces risk, never increases it
    notional = qty * fe
    if notional < s.min_notional:
        raise RiskRejected(f"position notional {notional:.2f} below minimum {s.min_notional}")
    return Sizing(qty=qty, fill_entry=fe, notional=notional, risk_usd=qty * loss_per_unit,
                  loss_per_unit=loss_per_unit, reward_per_unit=reward_per_unit, reward_risk=rr)


def pre_trade_checks(*, symbol: str, side: str, account: dict, open_positions: list[dict],
                     trades_today: int, last_close_ms_for_symbol: int | None, now_ms: int,
                     s: Settings) -> list[str]:
    """Return a list of rejection reasons (empty list == allowed)."""
    reasons = []
    if LIVE_TRADING_ENABLED:
        reasons.append("live trading flag detected -- refusing")
    if account.get("halted"):
        reasons.append(f"trading halted: {account['halted']}")
    if len(open_positions) >= s.max_open_positions:
        reasons.append(f"max {s.max_open_positions} open positions reached")
    if any(p["symbol"] == symbol for p in open_positions):
        reasons.append(f"duplicate: {symbol} position already open")
    if trades_today >= s.max_trades_per_day:
        reasons.append(f"max {s.max_trades_per_day} trades per day reached")
    if last_close_ms_for_symbol and now_ms - last_close_ms_for_symbol < s.symbol_cooldown_minutes * 60_000:
        reasons.append(f"{symbol} in {s.symbol_cooldown_minutes}-minute cooldown after last trade")

    equity = account["equity"]
    open_risk = sum(p["risk_usd"] for p in open_positions)
    new_risk = equity * s.risk_per_trade
    # Daily budget: today's loss so far + risk already at stake + new trade must stay within limit.
    day_start = account["day_start_equity"]
    day_loss = max(0.0, day_start - equity)
    if day_loss + open_risk + new_risk > day_start * s.max_daily_loss + 1e-9:
        reasons.append(f"daily loss budget: lost {day_loss:.2f} + open risk {open_risk:.2f} + new {new_risk:.2f} "
                       f"> {s.max_daily_loss:.0%} of {day_start:.2f}")
    # Drawdown budget from peak equity.
    peak = account["peak_equity"]
    dd = max(0.0, peak - equity)
    if dd + open_risk + new_risk > peak * s.max_drawdown + 1e-9:
        reasons.append(f"drawdown budget: drawdown {dd:.2f} + open risk {open_risk:.2f} + new {new_risk:.2f} "
                       f"> {s.max_drawdown:.0%} of peak {peak:.2f}")
    return reasons
