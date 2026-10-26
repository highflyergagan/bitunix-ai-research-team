"""User-reversal research: rule fires on a lower-high breakdown, not on a still-rising pump; full run on a fake cache."""
import json
import os
import sqlite3
import tempfile
import unittest

from app import reversal_research as RR
from tests.test_pump_agent import pump_series

P = {"min_pump": 0.3, "min_off_top": 0.05, "stop_buffer": 0.005, "trail_pct": 0.06}


class RuleTests(unittest.TestCase):
    def test_fires_on_first_close_below_lower_high_low(self):
        c = pump_series(0, retest=False)  # top 2.70, lower high 2.52 (low 2.36), breakdown close 2.10...
        hits = [k for k in range(120, len(c)) if RR.user_reversal(c[:k + 1], c[k]["close"] / c[k - 96]["close"] - 1, P)]
        self.assertTrue(hits)
        s = RR.user_reversal(c[:hits[0] + 1], 1.2, P)
        self.assertAlmostEqual(s["stop_loss"], 2.52 * 1.005)
        self.assertGreater(s["stop_loss"], s["entry"])
        self.assertEqual(s["trail_pct"], 0.06)

    def test_no_signal_while_pumping(self):
        c = pump_series(0)[:145]  # still at / near the top
        self.assertIsNone(RR.user_reversal(c, 1.4, P))

    def test_run_on_fake_cache(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cache.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE bt_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c = pump_series(0, retest=False)
        tail = c[-1]["close"]
        c += [{"time": c[-1]["time"] + 900_000 * (i + 1), "open": tail, "high": tail * 1.002, "low": tail * 0.97,
               "close": tail * 0.98, "quote_volume": 1} for i in range(200)]
        con.execute("INSERT INTO bt_cache VALUES(?,?)", ("PUMPUSDT|15m|0|1", json.dumps(c)))
        con.commit()
        con.close()
        res = RR.run(path)
        self.assertEqual(res["windows"], 1)
        self.assertEqual(set(res["results"]), {"slippage_0.002", "slippage_0.003", "slippage_0.005"})


if __name__ == "__main__":
    unittest.main()
