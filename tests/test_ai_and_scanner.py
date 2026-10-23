"""Claude reviewer safeguards and end-to-end scanner behaviour (with a fake market client)."""
import json
import threading
import unittest

from app.ai_review import ClaudeReviewer
from app.scanner import Scanner
from app.store import Store
from tests.helpers import M1, Clock, FakeClient, settings

SIG = {"symbol": "BTCUSDT", "bias": "LONG BIAS", "entry": 100, "stop_loss": 99, "take_profit": 102.5,
       "reasons": ["x"], "indicators": {}, "data_gaps": ["news"]}
ACCT = {"equity": 100, "open_positions": 0, "day_pnl": 0, "drawdown_pct": 0}


class FakeResp:
    def __init__(self, status=200, text='{"decision":"APPROVE","confidence":0.8,"reasons":["ok"]}', usage=(800, 60)):
        self.status_code = status
        self._j = {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": text}],
                   "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}

    def json(self):
        return self._j


class ReviewerTests(unittest.TestCase):
    def mk(self, resp=None, exc=None, **kw):
        s = settings(ai_mode="required", anthropic_api_key="sk-ant-test-SECRET", **kw)
        self.store = Store(s.db_path)
        self.calls = []

        def post(url, headers, json, timeout):
            self.calls.append((url, headers, json))
            if exc:
                raise exc
            return resp or FakeResp()
        return ClaudeReviewer(self.store, s, Clock(), post)

    def test_approve_and_cost_tracking(self):
        r = self.mk().review(SIG, ACCT)
        self.assertEqual(r["decision"], "APPROVE")
        self.assertAlmostEqual(r["cost_usd"], 800 / 1e6 * 0.10 + 60 / 1e6 * 0.50)
        self.assertEqual(self.calls[0][2]["model"], "claude-haiku-5-5")

    def test_missing_key_rejects_when_required(self):
        s = settings(ai_mode="required", anthropic_api_key="")
        r = ClaudeReviewer(Store(s.db_path), s, Clock()).review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")

    def test_http_error_rejects(self):
        self.assertEqual(self.mk(FakeResp(status=529)).review(SIG, ACCT)["decision"], "REJECT")

    def test_exception_rejects(self):
        self.assertEqual(self.mk(exc=TimeoutError("slow")).review(SIG, ACCT)["decision"], "REJECT")

    def test_garbage_reply_rejects(self):
        self.assertEqual(self.mk(FakeResp(text="I think it goes up!")).review(SIG, ACCT)["decision"], "REJECT")

    def test_low_confidence_rejects(self):
        r = self.mk(FakeResp(text='{"decision":"APPROVE","confidence":0.4,"reasons":["meh"]}')).review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")

    def test_daily_call_cap(self):
        rv = self.mk(claude_max_calls_per_day=2)
        for _ in range(2):
            rv.review(SIG, ACCT)
        r = rv.review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")
        self.assertEqual(len(self.calls), 2)

    def test_api_key_never_stored(self):
        rv = self.mk(exc=RuntimeError("boom"))
        rv.review(SIG, ACCT)
        dump = json.dumps(self.store.query("SELECT * FROM ai_decisions") + self.store.query("SELECT * FROM events"))
        self.assertNotIn("SECRET", dump)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.s = settings(ai_mode="off")
        self.clock = Clock()
        self.client = FakeClient(self.clock)
        self.store = Store(self.s.db_path)
        self.sc = Scanner(self.store, self.s, client=self.client, clock=self.clock)

    def test_scan_generates_signals_and_opens_paper_trades(self):
        r = self.sc.scan_once()
        self.assertEqual(r["status"], "ok", r["errors"])
        self.assertEqual(r["symbols_scanned"], 3)
        biases = {x["symbol"]: x["bias"] for x in self.store.query("SELECT symbol,bias FROM signals")}
        self.assertEqual(biases["SOLUSDT"], "NO TRADE")
        self.assertEqual(biases["BTCUSDT"], "LONG BIAS")
        opens = self.sc.broker.open_positions()
        self.assertEqual({p["symbol"] for p in opens}, {"BTCUSDT", "ETHUSDT"})
        self.assertIsNotNone(self.store.get("last_successful_scan"))

    def test_second_scan_does_not_duplicate(self):
        self.sc.scan_once()
        self.clock.t += 5 * M1
        r = self.sc.scan_once()
        self.assertEqual(r["opened"], 0)
        self.assertEqual(len(self.sc.broker.open_positions()), 2)

    def test_exit_from_subsequent_market_candles(self):
        self.sc.scan_once()
        p = next(x for x in self.sc.broker.open_positions() if x["symbol"] == "BTCUSDT")
        t = p["opened_at"] - p["opened_at"] % M1 + M1
        self.client.minute_candles["BTCUSDT"] = [
            {"time": t, "open": p["entry_price"], "high": p["take_profit"] * 1.001, "low": p["entry_price"] * 0.999,
             "close": p["take_profit"], "quote_volume": 1}]
        self.clock.t = t + 2 * M1
        r = self.sc.scan_once()
        self.assertEqual(r["closed"], 1)
        closed = self.sc.broker.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "take-profit")
        self.assertGreater(closed["net_pnl"], 0)

    def test_ai_rejection_blocks_trade(self):
        s = settings(ai_mode="required", anthropic_api_key="")  # no key -> conservative reject
        sc = Scanner(Store(s.db_path), s, client=FakeClient(self.clock), clock=self.clock)
        r = sc.scan_once()
        self.assertEqual(r["opened"], 0)
        self.assertGreater(r["candidates"], 0)

    def test_api_outage_recorded_not_crashing(self):
        self.client.fail_tickers = True
        r = self.sc.scan_once()
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.store.one("SELECT status FROM scans ORDER BY id DESC")["status"], "failed")

    def test_overlapping_scans_prevented(self):
        self.sc._scan_lock.acquire()
        try:
            self.assertEqual(self.sc.scan_once()["status"], "skipped")
        finally:
            self.sc._scan_lock.release()

    def test_report_after_scans(self):
        from app.report import build_report, start_experiment
        start_experiment(self.store, 100.0, 24, self.clock)
        self.sc.scan_once()
        rep = build_report(self.store, self.s, self.clock)
        self.assertEqual(rep["scans"]["completed"], 1)
        self.assertEqual(rep["paper_trades"]["opened"], 2)
        self.assertEqual(rep["paper_trades"]["closed"], 0)
        self.assertIn("PAPER", rep["mode"])


if __name__ == "__main__":
    unittest.main()
