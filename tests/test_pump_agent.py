"""100% Pump Exhaustion SHORT Agent: signal rules, safety gates, shadow/validated execution, backtest engine."""
import unittest

from app import pump_backtest as BT
from app.pump_agent import PumpExhaustionAgent, book_from_depth, evaluate, short_net_rr
from app.scanner import Scanner
from app.store import Store
from tests.helpers import M15, Clock, FakeClient, settings

GOOD_BOOK = {"spread": 0.0008, "bid_depth_usd": 50_000, "ask_depth_usd": 50_000, "bid": 2.13, "ask": 2.132}


def pump_series(t0: int, retest: bool = True) -> list[dict]:
    """Flat at 1.0, +140% pump with heavy volume, rejection wick at the top, swing low 2.18, lower high 2.52,
    15m close below 2.18 (breakdown), then (optionally) a retest of 2.18 that fails."""
    rows = []

    def add(o, h, l, c, v):
        rows.append({"time": t0 + len(rows) * M15, "open": o, "high": h, "low": l, "close": c, "quote_volume": v})

    for i in range(120):
        p = 1.0 + 0.002 * ((i % 4) - 1.5)
        add(p, p * 1.002, p * 0.998, p, 100_000)
    p = 1.0
    for i in range(24):
        c = 1.0 + 1.4 * (i + 1) / 24
        add(p, c * 1.004, p * 0.996, c, 2_000_000)
        p = c
    add(2.40, 2.70, 2.38, 2.45, 3_000_000)          # top with a long rejection wick
    for c in (2.35, 2.25):
        add(rows[-1]["close"], rows[-1]["close"] * 1.004, c * 0.995, c, 300_000)
    add(2.25, 2.26, 2.18, 2.20, 300_000)            # swing low 2.18
    add(2.20, 2.32, 2.21, 2.30, 300_000)
    add(2.30, 2.42, 2.28, 2.40, 300_000)
    add(2.40, 2.52, 2.36, 2.44, 300_000)            # lower high 2.52
    add(2.44, 2.45, 2.33, 2.35, 300_000)
    add(2.35, 2.36, 2.24, 2.25, 300_000)
    add(2.25, 2.26, 2.08, 2.10, 300_000)            # breakdown close 2.10 < 2.18
    add(2.10, 2.14, 2.07, 2.12, 300_000)
    if retest:
        add(2.12, 2.18, 2.11, 2.13, 300_000)        # retest touches 2.18 and fails
    return rows


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.c = pump_series(1_700_000_000_000)
        self.now = self.c[-1]["time"] + M15 + 60_000

    def ev(self, c=None, **kw):
        args = dict(funding_rate=0.0001, book=GOOD_BOOK, quote_volume=50e6, now=self.now)
        args.update(kw)
        return evaluate("PUMPUSDT", c or self.c, **args)

    def test_short_after_breakdown_and_failed_retest(self):
        s = self.ev()
        self.assertEqual(s["bias"], "SHORT BIAS", s["blockers"])
        self.assertTrue(s["take_profit"] < s["entry"] < s["stop_loss"])
        self.assertGreaterEqual(short_net_rr(s["entry"], s["stop_loss"], s["take_profit"], 0.005, 0.0006), 2.0)
        self.assertGreater(s["stop_loss"], 2.52)  # above the lower-high swing (structure) + ATR
        ev = s["evidence"]
        self.assertTrue(ev["breakdown_and_failed_retest"] and ev["abnormal_volume"] and ev["lower_high"])
        self.assertTrue(any("failed" in r for r in s["reasons"]))
        self.assertIn("verified news / catalyst feed (not connected)", s["data_gaps"])

    def test_no_retest_means_no_trade(self):
        s = self.ev(pump_series(1_700_000_000_000, retest=False))
        self.assertEqual(s["bias"], "NO TRADE")
        self.assertTrue(any("breakdown + failed retest" in b for b in s["blockers"]))

    def test_threshold(self):
        s = self.ev(threshold=1.5)
        self.assertEqual(s["bias"], "NO TRADE")
        self.assertTrue(any("below the" in b for b in s["blockers"]))

    def test_squeeze_funding_blocks(self):
        s = self.ev(funding_rate=-0.002)
        self.assertEqual(s["bias"], "NO TRADE")
        self.assertTrue(any("squeeze" in b for b in s["blockers"]))

    def test_spread_depth_and_missing_book_block(self):
        self.assertEqual(self.ev(book=dict(GOOD_BOOK, spread=0.01))["bias"], "NO TRADE")
        self.assertEqual(self.ev(book=dict(GOOD_BOOK, bid_depth_usd=500))["bias"], "NO TRADE")
        s = self.ev(book=None)
        self.assertTrue(any("order book unavailable" in b for b in s["blockers"]))
        self.assertTrue(any("funding rate unavailable" in b for b in self.ev(funding_rate=None)["blockers"]))

    def test_low_liquidity_blocks(self):
        self.assertTrue(any("low liquidity" in b for b in self.ev(quote_volume=100_000)["blockers"]))

    def test_stale_and_gappy_data_block(self):
        self.assertTrue(any("stale" in b for b in self.ev(now=self.now + 3_600_000)["blockers"]))
        gappy = [dict(x) for x in self.c]
        gappy[-20]["time"] += 60_000
        self.assertTrue(any("gaps" in b for b in self.ev(gappy)["blockers"]))

    def test_book_parser(self):
        b = book_from_depth({"bids": [["0.006649", "556120"]], "asks": [["0.006652", "357454"]]})
        self.assertAlmostEqual(b["spread"], 0.003 / 6.6505, places=5)
        self.assertIsNone(book_from_depth({"bids": [], "asks": []}))


class PumpClient(FakeClient):
    def __init__(self, clock, series):
        super().__init__(clock)
        self.series = series

    def tickers(self):
        rows = super().tickers()
        last = self.series[-1]["close"]
        return rows + [{"symbol": "PUMPUSDT", "open": "1.0", "lastPrice": str(last), "quoteVol": "50000000"}]

    def klines(self, symbol, interval="15m", limit=200, start_ms=None, end_ms=None):
        if symbol == "PUMPUSDT":
            self.request_count += 1
            return [] if interval == "1m" else self.series[-limit:]
        return super().klines(symbol, interval, limit, start_ms, end_ms)

    def depth(self, symbol, limit=5):
        return {"bids": [["2.129", "50000"]], "asks": [["2.131", "50000"]]}


class StubReviewer:
    def __init__(self, decision):
        self.decision, self.calls = decision, 0

    def review(self, signal, account, signal_id=None):
        self.calls += 1
        assert signal["review_context"]["agent"]
        return {"id": 1, "decision": self.decision, "confidence": 0.8, "reasons": ["stub"]}


class AgentTests(unittest.TestCase):
    def make(self, decision="APPROVE"):
        self.s = settings()
        series = pump_series(0)
        clock = Clock(series[-1]["time"] + M15 + 60_000)
        self.series = series
        self.clock = clock
        self.rv = StubReviewer(decision)
        self.agent = PumpExhaustionAgent(self.s, PumpClient(clock, series), self.rv, lambda *a: [], Store(self.s.db_path), clock)
        return self.agent

    def tick(self):
        return self.agent.client.tickers()

    def test_shadow_mode_records_but_never_executes(self):
        a = self.make()
        r = a.run(self.tick(), {})
        self.assertEqual((r["signals"], r["opened"]), (1, 0), r)
        ev = a.summary()["evaluations"][0]
        self.assertEqual(ev["decision"], "SHORT")
        self.assertIn("shadow", ev["action"])
        self.assertEqual(self.rv.calls, 0)  # no AI budget spent while unvalidated
        self.assertEqual(a.broker.open_positions(), [])

    def test_validated_and_approved_executes_in_own_ledger(self):
        a = self.make("APPROVE")
        a.store.set("validation", {"validated": True, "reason": "test"})
        r = a.run(self.tick(), {"PUMPUSDT": 2.13})
        self.assertEqual(r["opened"], 1, a.summary()["evaluations"][0])
        p = a.broker.open_positions()[0]
        self.assertEqual(p["side"], "SHORT")
        self.assertLessEqual(p["risk_usd"], 0.5 + 1e-9)  # 0.5% of $100
        self.assertEqual(self.rv.calls, 1)
        self.assertEqual(Store(self.s.db_path).query("SELECT * FROM positions"), [])  # main ledger untouched

    def test_ai_reject_blocks(self):
        a = self.make("REJECT")
        a.store.set("validation", {"validated": True})
        self.assertEqual(a.run(self.tick(), {})["opened"], 0)
        self.assertEqual(a.summary()["evaluations"][0]["action"], "rejected by AI committee")

    def test_risk_agent_blocks_before_ai(self):
        a = self.make()
        a.store.set("validation", {"validated": True})
        a.store.set("halted", "max drawdown test")
        a.run(self.tick(), {})
        self.assertIn("risk agent", a.summary()["evaluations"][0]["action"])
        self.assertEqual(self.rv.calls, 0)

    def test_scanner_integration_does_not_touch_main_or_lab(self):
        s = settings()
        series = pump_series(0)
        clock = Clock(series[-1]["time"] + M15 + 60_000)
        sc = Scanner(Store(s.db_path), s, client=PumpClient(clock, series), clock=clock)
        r = sc.scan_once()
        self.assertIn(r["status"], ("ok", "partial"))
        self.assertEqual(r["pump_agent"]["signals"], 1)
        self.assertEqual(r["pump_agent"]["opened"], 0)  # shadow


class BacktestTests(unittest.TestCase):
    def test_simulate_target_and_stop(self):
        c = pump_series(0)
        sig = evaluate("P", c, live_checks=False)
        i = len(c) - 1
        down = [{"time": c[-1]["time"] + M15 * (k + 1), "open": 2.12, "high": 2.13, "low": 1.0, "close": 1.05,
                 "quote_volume": 1} for k in range(2)]
        win = BT.simulate("P", c + down, [(i, 1.13, sig)], 1.0)
        self.assertEqual(win[0]["reason"], "target")
        self.assertGreater(win[0]["r"], 1.9)
        up = [{"time": c[-1]["time"] + M15 * (k + 1), "open": 2.12, "high": 3.0, "low": 2.1, "close": 2.9,
               "quote_volume": 1} for k in range(2)]
        loss = BT.simulate("P", c + up, [(i, 1.13, sig)], 1.0)
        self.assertTrue(loss[0]["reason"].startswith("stop"))
        self.assertLess(loss[0]["r"], -0.95)
        self.assertEqual(BT.simulate("P", c + up, [(i, 1.13, sig)], 1.5), [])  # below threshold

    def test_incomplete_trades_are_excluded(self):
        c = pump_series(0)
        sig = evaluate("P", c, live_checks=False)
        flat = [{"time": c[-1]["time"] + M15 * (k + 1), "open": 2.12, "high": 2.14, "low": 2.1, "close": 2.12,
                 "quote_volume": 1} for k in range(10)]
        self.assertEqual(BT.simulate("P", c + flat, [(len(c) - 1, 1.13, sig)], 1.0), [])

    def test_metrics(self):
        tr = [{"r": 2.0, "exit_time": 1}, {"r": -1.0, "exit_time": 2}, {"r": -1.0, "exit_time": 3}]
        m = BT.metrics(tr)
        self.assertEqual(m["trades"], 3)
        self.assertAlmostEqual(m["expectancy_r"], 0.0)
        self.assertEqual(m["profit_factor"], 1.0)
        self.assertGreater(m["max_drawdown_pct"], 0.9)

    def test_pump_ranges_merge(self):
        H4 = 14_400_000
        c4 = [{"time": k * H4, "open": 1.0, "close": 1.0} for k in range(40)]
        c4[10] = dict(c4[10], close=2.0)
        c4[12] = dict(c4[12], close=2.0)
        r = BT.pump_ranges(c4)
        self.assertEqual(len(r), 1)

    def test_full_run_with_fake_data_is_not_validated_on_tiny_sample(self):
        s = settings(pump_backtest_days=30)

        class HistClient:
            request_count = 0

            def tickers(self):
                return [{"symbol": "PUMPUSDT"}]

            def klines(self, symbol, interval, limit=200, start_ms=None, end_ms=None):
                return []

        st = Store(s.db_path.replace("paper.db", "agent.db"))
        res = BT.run_backtest(s, st, client=HistClient(), clock=lambda: 1_760_000_000_000)
        self.assertEqual(res["pump_windows"], 0)
        self.assertFalse(res["validation"]["validated"])
        self.assertIn("failed", res["validation"]["reason"])
        self.assertEqual(st.get("backtest_status")["state"], "done")


if __name__ == "__main__":
    unittest.main()
