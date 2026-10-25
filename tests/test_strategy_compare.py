"""Strategy comparison research module: trade simulation, sweep rule, AI replay, ranking, end-to-end (fake data)."""
import json
import math
import unittest

from app import strategy_compare as SC
from app.store import Store
from tests.helpers import M15, settings

H = 3_600_000


def bar(t, o, h, l, c, v=1_000_000):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "quote_volume": v}


def wavy(n, t0=0, base=100.0, amp=0.01):
    out = []
    for i in range(n):
        c = base * (1 + amp * math.sin(i * 0.35))
        o = out[-1]["close"] if out else base
        out.append(bar(t0 + i * M15, o, max(o, c) * 1.001, min(o, c) * 0.999, c))
    return out


class SimTests(unittest.TestCase):
    def test_long_target_stop_invalid_incomplete(self):
        c = [bar(i * M15, 100, 101, 99.5, 100.5) for i in range(5)] + [bar(5 * M15, 100.5, 104, 100, 103.5)]
        tr = SC.sim_trade(c, 0, "LONG", 98, 103, 0.001, max_bars=6)
        self.assertEqual(tr["reason"], "target")
        self.assertGreater(tr["r"], 1.0)
        c2 = [bar(0, 100, 100.2, 97, 97.5)]
        self.assertTrue(SC.sim_trade(c2, 0, "LONG", 98, 103, 0.001, max_bars=1)["reason"].startswith("stop"))
        self.assertIsNone(SC.sim_trade(c2, 0, "LONG", 101, 103, 0.001))      # stop above entry -> invalid
        self.assertIsNone(SC.sim_trade(c[:3], 0, "LONG", 90, 120, 0.001))     # data ends before 24h -> excluded

    def test_short_costs_make_flat_trade_negative(self):
        c = [bar(i * M15, 100, 100.1, 99.9, 100) for i in range(96)]
        tr = SC.sim_trade(c, 0, "SHORT", 102, 95, 0.001)
        self.assertEqual(tr["reason"], "24h time stop")
        self.assertLess(tr["r"], 0)  # fees + slippage + funding


class SweepTests(unittest.TestCase):
    def test_bullish_sweep_detected(self):
        c = wavy(80)
        lo = min(x["low"] for x in c[-49:-1])
        c.append(bar(80 * M15, lo * 1.004, lo * 1.006, lo * 0.99, lo * 1.005, 5_000_000))
        s = SC.liquidity_sweep(c)
        self.assertIsNotNone(s)
        self.assertEqual(s["side"], "LONG")
        self.assertLess(s["stop"], lo * 0.99)
        self.assertGreater(s["tp"], c[-1]["close"])

    def test_no_sweep_without_volume(self):
        c = wavy(80)
        lo = min(x["low"] for x in c[-49:-1])
        c.append(bar(80 * M15, lo * 1.004, lo * 1.006, lo * 0.99, lo * 1.005, 100))
        self.assertIsNone(SC.liquidity_sweep(c))


class FakeHist:
    request_count = 0

    def __init__(self, n=400):
        self.c15 = wavy(n, t0=1_700_000_000_000 - (1_700_000_000_000 % H))

    def tickers(self):
        return [{"symbol": "BTCUSDT", "quoteVol": "9e9"}]

    def klines(self, symbol, interval, limit=200, start_ms=None, end_ms=None):
        src = self.c15 if interval == "15m" else []
        rows = [c for c in src if end_ms is None or c["time"] < end_ms]
        return rows[-limit:]

    def funding_rate(self, s):
        return 0.0003


class ReplayAndRunTests(unittest.TestCase):
    def test_ai_replay_splits_approved_and_rejected(self):
        s = settings()
        st = Store(s.db_path)
        fh = FakeHist()
        ts = fh.c15[10]["time"]
        p = {"stop_loss": 90.0, "take_profit": 300.0}
        for i, dec in enumerate(("APPROVE", "REJECT")):
            sid = st.execute("INSERT INTO signals(ts,symbol,bias,payload) VALUES(?,?,?,?)",
                             (ts + i * 2 * H, "BTCUSDT", "LONG BIAS", json.dumps(p))).lastrowid
            st.execute("INSERT INTO ai_decisions(ts,symbol,signal_id,decision,reasons,model,provider) VALUES(?,?,?,?,?,?,?)",
                       (ts, "BTCUSDT", sid, dec, json.dumps(["x"]), "m", "claude"))
        res = SC.ai_decision_replay(st, fh, None, clock=lambda: fh.c15[-1]["time"] + M15)
        self.assertEqual(res["setups_judged"], 2)
        self.assertEqual(res["ai_approved"]["trades"], 1)
        self.assertEqual(res["ai_rejected_counterfactual"]["trades"], 1)
        self.assertEqual(res["deterministic_take_all"]["trades"], 2)

    def test_end_to_end_reports_honestly(self):
        s = settings()
        fh = FakeHist(n=2000)
        main = Store(s.db_path)
        out = Store(s.db_path.replace("paper.db", "cmp.db"))
        pump = Store(s.db_path.replace("paper.db", "pump.db"))
        res = SC.run_compare(s, main, pump, out, client=fh, clock=lambda: fh.c15[-1]["time"] + M15,
                             symbols=["BTCUSDT"], days=15)
        self.assertNotIn("error", res, res.get("trace"))
        keys = [e["key"] for e in res["strategies"]]
        self.assertEqual(set(keys), {"liquidity_sweep", "trend_following", "pump_exhaustion", "funding_basis"})
        self.assertIn("NO strategy is profitable", res["headline"])  # tiny sample can never qualify
        fb = next(e for e in res["strategies"] if e["key"] == "funding_basis")
        self.assertFalse(fb["backtested"])
        self.assertEqual(fb["snapshot"][0]["funding_8h_pct"], 0.03)
        self.assertEqual(out.get("compare_status")["state"], "done")
        self.assertIsNone(main.get("halted"))  # nothing touched in the main ledger
        self.assertEqual(main.query("SELECT * FROM positions"), [])

    def test_rank_orders_by_oos_expectancy(self):
        def e(k, exp, n=40, pf=1.2):
            return {"key": k, "name": k, "backtested": True, "data_quality": {"score": 50},
                    "out_of_sample": {"trades": n, "expectancy_r": exp, "profit_factor": pf, "max_drawdown_pct": 3}}
        r = SC.rank([e("a", -0.1, pf=0.8), e("b", 0.2), {"key": "f", "backtested": False}])
        self.assertEqual([x["key"] for x in r], ["b", "a", "f"])
        self.assertTrue(r[0]["profitable_out_of_sample"])
        self.assertFalse(r[1]["profitable_out_of_sample"])


if __name__ == "__main__":
    unittest.main()
