"""Paper account, risk engine, and exit logic."""
import sqlite3
import unittest

from app.paper import PaperBroker
from app.risk import RiskRejected, size_position
from app.store import Store
from tests.helpers import M1, Clock, settings


def sig(symbol="BTCUSDT", bias="LONG BIAS", entry=100.0, sl=99.0, tp=102.5):
    return {"symbol": symbol, "bias": bias, "entry": entry, "stop_loss": sl, "take_profit": tp}


def candle(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "quote_volume": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.clock = Clock()
        self.store = Store(self.s.db_path)
        self.b = PaperBroker(self.store, self.s, self.clock)


class SizingTests(Base):
    def test_risk_is_half_percent_including_costs(self):
        sz = size_position("BTCUSDT", "LONG", 100, 99, 102.5, 100.0, 300, self.s)
        self.assertAlmostEqual(sz.risk_usd, 0.5, places=6)
        # loss at stop including fees/slippage equals planned risk
        self.assertAlmostEqual(sz.qty * sz.loss_per_unit, 0.5, places=6)

    def test_leverage_cap_reduces_size(self):
        sz = size_position("BTCUSDT", "LONG", 100, 99.5, 102, 100.0, 20, self.s)
        self.assertLessEqual(sz.notional, 20 + 1e-9)
        self.assertLess(sz.risk_usd, 0.5)

    def test_requires_valid_sl_tp(self):
        for args in [(100, None, 102), (100, 99, None), (100, 101, 102), (100, 99, 99.5)]:
            with self.assertRaises(RiskRejected):
                size_position("BTCUSDT", "LONG", args[0], args[1], args[2], 100, 300, self.s)
        with self.assertRaises(RiskRejected):
            size_position("BTCUSDT", "SHORT", 100, 99, 98, 100, 300, self.s)

    def test_rejects_poor_reward_risk_after_costs(self):
        with self.assertRaises(RiskRejected):
            size_position("BTCUSDT", "LONG", 100, 99.8, 100.3, 100, 300, self.s)


class AccountTests(Base):
    def test_starts_at_100(self):
        a = self.b.account()
        self.assertEqual(a["equity"], 100.0)
        self.assertEqual(a["mode"], "PAPER TRADING ONLY")
        self.assertFalse(a["live_trading_enabled"])

    def test_no_trade_signal_rejected(self):
        with self.assertRaises(RiskRejected):
            self.b.open_position(sig(bias="NO TRADE"))

    def test_max_two_positions_and_no_duplicates(self):
        self.b.open_position(sig("BTCUSDT"))
        with self.assertRaises(RiskRejected):
            self.b.open_position(sig("BTCUSDT"))
        self.b.open_position(sig("ETHUSDT"))
        with self.assertRaises(RiskRejected) as cm:
            self.b.open_position(sig("SOLUSDT"))
        self.assertIn("max 2", str(cm.exception))

    def test_database_blocks_duplicate_open_symbol(self):
        self.b.open_position(sig("BTCUSDT"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute("INSERT INTO positions(symbol,side,status,qty,signal_price,entry_price,stop_loss,take_profit,"
                               "risk_usd,opened_at,last_checked_ms) VALUES('BTCUSDT','LONG','OPEN',1,1,1,0.9,1.2,0.1,1,1)")

    def test_take_profit_requires_real_price(self):
        p = self.b.open_position(sig())
        # later candles never reach the target -> stays open, no profit booked
        cs = [candle(p["opened_at"] + 60_000 * i, 100, 101, 99.5, 100.5) for i in range(1, 5)]
        self.clock.t = cs[-1]["time"] + M1
        after = self.b.process_candles(p, cs, M1)
        self.assertEqual(after["status"], "OPEN")
        self.assertEqual(self.b.account()["balance"], 100.0)

    def test_take_profit_hit_books_net_profit(self):
        p = self.b.open_position(sig())
        t = p["opened_at"] + 60_000
        self.clock.t = t + 5 * M1
        after = self.b.process_candles(p, [candle(t, 101, 102.6, 100.8, 102.4)], M1)
        self.assertEqual(after["status"], "CLOSED")
        self.assertEqual(after["exit_reason"], "take-profit")
        self.assertGreater(after["net_pnl"], 0)
        self.assertLess(after["net_pnl"], after["gross_pnl"])  # fees deducted
        self.assertAlmostEqual(self.b.account()["balance"], 100 + after["net_pnl"], places=3)

    def test_stop_hit_loses_about_planned_risk(self):
        p = self.b.open_position(sig())
        t = p["opened_at"] + 60_000
        self.clock.t = t + 5 * M1
        after = self.b.process_candles(p, [candle(t, 99.6, 99.7, 98.9, 99.0)], M1)
        self.assertEqual(after["exit_reason"], "stop-loss")
        self.assertAlmostEqual(after["net_pnl"], -0.5, delta=0.02)

    def test_same_candle_sl_and_tp_assumes_stop(self):
        p = self.b.open_position(sig())
        t = p["opened_at"] + 60_000
        self.clock.t = t + 5 * M1
        after = self.b.process_candles(p, [candle(t, 100, 103, 98.5, 102)], M1)
        self.assertTrue(after["exit_reason"].startswith("stop-loss"))
        self.assertLess(after["net_pnl"], 0)

    def test_gap_through_stop_fills_at_open(self):
        p = self.b.open_position(sig())
        t = p["opened_at"] + 60_000
        self.clock.t = t + 5 * M1
        after = self.b.process_candles(p, [candle(t, 97.0, 97.5, 96.5, 97.2)], M1)
        self.assertEqual(after["exit_reason"], "stop-loss (gap)")
        self.assertLess(after["net_pnl"], -0.5)  # gap loss exceeds planned risk

    def test_candles_before_entry_are_ignored(self):
        p = self.b.open_position(sig())
        self.clock.t = p["opened_at"] + 5 * M1
        before = candle(p["opened_at"] - 120_000, 100, 110, 90, 100)
        after = self.b.process_candles(p, [before], M1)
        self.assertEqual(after["status"], "OPEN")

    def test_short_position(self):
        p = self.b.open_position(sig(bias="SHORT BIAS", entry=100, sl=101, tp=97.5))
        t = p["opened_at"] + 60_000
        self.clock.t = t + 5 * M1
        after = self.b.process_candles(p, [candle(t, 99, 99.2, 97.4, 97.6)], M1)
        self.assertEqual(after["exit_reason"], "take-profit")
        self.assertGreater(after["net_pnl"], 0)

    def test_time_stop(self):
        p = self.b.open_position(sig())
        self.clock.t = p["opened_at"] + int(25 * 3_600_000)
        after = self.b.process_candles(p, [], M1)
        self.assertTrue(after["exit_reason"].startswith("time stop"))

    def test_state_persists_across_restart(self):
        self.b.open_position(sig())
        b2 = PaperBroker(Store(self.s.db_path), self.s, self.clock)
        self.assertEqual(len(b2.open_positions()), 1)
        self.assertEqual(b2.account()["starting_balance"], 100.0)

    def test_cooldown_after_close(self):
        p = self.b.open_position(sig())
        t = p["opened_at"] + 60_000
        self.clock.t = t + M1
        self.b.process_candles(p, [candle(t, 99.6, 99.7, 98.9, 99.0)], M1)
        with self.assertRaises(RiskRejected) as cm:
            self.b.open_position(sig())
        self.assertIn("cooldown", str(cm.exception))

    def test_daily_loss_budget_blocks_new_trades(self):
        self.store.set("balance", 98.3)  # down 1.7% today
        reasons = self.b.check("BTCUSDT", "LONG")
        self.assertTrue(any("daily loss budget" in r for r in reasons))

    def test_daily_loss_limit_halts(self):
        self.store.set("balance", 97.9)
        a = self.b.roll_day_and_limits()
        self.assertIn("daily loss", a["halted"])
        with self.assertRaises(RiskRejected):
            self.b.open_position(sig())

    def test_daily_halt_clears_next_day(self):
        self.store.set("balance", 97.9)
        self.b.roll_day_and_limits()
        self.clock.t += 24 * 3_600_000
        self.assertIsNone(self.b.roll_day_and_limits()["halted"])

    def test_drawdown_kill_switch_closes_everything(self):
        self.b.open_position(sig())
        self.store.set("balance", 94.9)
        a = self.b.roll_day_and_limits()
        self.assertIn("max drawdown", a["halted"])
        self.assertEqual(len(self.b.open_positions()), 0)
        self.clock.t += 24 * 3_600_000
        self.assertIn("max drawdown", self.b.roll_day_and_limits()["halted"])  # needs manual reset

    def test_close_is_idempotent(self):
        p = self.b.open_position(sig())
        self.b.close_position(p, 101, "manual", self.clock())
        bal = self.b.account()["balance"]
        self.b.close_position(p, 101, "manual", self.clock())
        self.assertEqual(self.b.account()["balance"], bal)


if __name__ == "__main__":
    unittest.main()
