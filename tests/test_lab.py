"""Strategy Lab: separate accounts, strategy rules, isolation from the main account."""
import math
import unittest

from app import lab as L
from app.scanner import Scanner
from app.store import Store
from app.strategy import LONG, SHORT
from tests.helpers import M15, Clock, FakeClient, settings, trend_candles

H = 3_600_000


def flat_1h(end, n=200, price=100.0):
    return [{"time": end - (n - i) * H, "open": price, "high": price * 1.002, "low": price * 0.998, "close": price,
             "quote_volume": 1e6} for i in range(n)]


def wiggle_15m(end, n=200, price=100.0, amp=0.004, last_move=0.0, vol_boost=1.0):
    out = []
    for i in range(n):
        c = price * (1 + amp * math.sin(i * 0.9))
        if i == n - 1:
            c = price * (1 + last_move)
        o = out[-1]["close"] if out else price
        out.append({"time": end - (n - i) * M15, "open": o, "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                    "close": c, "quote_volume": 1e6 * (vol_boost if i >= n - 4 else 1.0)})
    return out


class LabRuleTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.end = Clock()()

    def m(self, c15, c1h=None, fr=0.0001):
        return {"c15": c15, "c1h": c1h or flat_1h(self.end), "funding_rate": fr, "quote_volume": 5e7}

    def test_mean_reversion_long_on_flush(self):
        sig = L.mean_reversion("X", self.m(wiggle_15m(self.end, last_move=-0.03)), self.s)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["bias"], LONG)
        self.assertLess(sig["stop_loss"], sig["entry"])
        self.assertGreater(sig["take_profit"], sig["entry"])

    def test_mean_reversion_quiet_market_none(self):
        self.assertIsNone(L.mean_reversion("X", self.m(wiggle_15m(self.end)), self.s))

    def test_breakout_needs_volume(self):
        self.assertIsNone(L.breakout_volume("X", self.m(wiggle_15m(self.end, last_move=0.03)), self.s))
        sig = L.breakout_volume("X", self.m(wiggle_15m(self.end, last_move=0.03, vol_boost=3)), self.s)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["bias"], LONG)

    def test_funding_extreme_contrarian(self):
        c15 = wiggle_15m(self.end, last_move=-0.012)
        sig = L.funding_extreme("X", self.m(c15, fr=0.001), self.s)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["bias"], SHORT)
        self.assertIsNone(L.funding_extreme("X", self.m(c15, fr=0.0001), self.s))
        self.assertIsNone(L.funding_extreme("X", self.m(c15, fr=None), self.s))

    def test_pump_fade(self):
        c = [{"time": i, "open": 1, "high": 1.0 + i * 0.02, "low": 0.99 + i * 0.02, "close": 1.0 + i * 0.02,
              "quote_volume": 1} for i in range(40)]
        self.assertIsNone(L.pump_fade("P", c, 1.0))  # still pumping -> no short
        top = c[-1]["close"]
        c += [{"time": 40 + i, "open": top, "high": top, "low": top * (0.9 - i * 0.01), "close": top * (0.9 - i * 0.01),
               "quote_volume": 1} for i in range(3)]
        sig = L.pump_fade("P", c, 1.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["bias"], SHORT)
        self.assertGreater(sig["stop_loss"], top)


class LabIntegrationTests(unittest.TestCase):
    def test_lab_accounts_separate_and_isolated(self):
        s = settings(ai_mode="required", anthropic_api_key="")  # main account: AI required but no key -> no trades
        clock = Clock()
        sc = Scanner(Store(s.db_path), s, client=FakeClient(clock), clock=clock)
        r = sc.scan_once()
        self.assertEqual(r["status"], "ok", r["errors"])
        self.assertEqual(r["opened"], 0)  # main account unaffected
        self.assertGreaterEqual(r["lab"]["trend_rules"]["opened"], 1)  # rules-only twin still trades
        summ = {x["key"]: x for x in sc.lab.summary()}
        self.assertEqual(set(summ), set(L.STRATEGIES))
        for x in summ.values():
            self.assertFalse(x["ai"])
        self.assertEqual(sc.broker.open_positions(), [])
        self.assertTrue(summ["trend_rules"]["open_positions"])

    def test_lab_failure_does_not_break_main_scan(self):
        s = settings()
        clock = Clock()
        sc = Scanner(Store(s.db_path), s, client=FakeClient(clock), clock=clock)
        sc.lab.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r = sc.scan_once()
        self.assertEqual(r["symbols_scanned"], 3)
        self.assertIn("strategy lab", "; ".join(r["errors"]))

    def test_each_lab_account_starts_at_100(self):
        s = settings()
        sc = Scanner(Store(s.db_path), s, client=FakeClient(Clock()))
        for x in sc.lab.summary():
            self.assertEqual(x["equity"], 100.0)


if __name__ == "__main__":
    unittest.main()
