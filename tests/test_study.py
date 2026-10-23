"""Study mode: pump/dump detection and follow-up (research only)."""
import unittest

from app.scanner import Scanner
from app.store import Store
from app.study import StudyMode
from tests.helpers import Clock, FakeClient, settings

H = 3_600_000


def tk(sym, open_, last, vol=1_000_000):
    return {"symbol": sym, "open": str(open_), "lastPrice": str(last), "quoteVol": str(vol), "high": str(max(open_, last)),
            "low": str(min(open_, last))}


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.clock = Clock()
        self.store = Store(self.s.db_path)
        self.st = StudyMode(self.store, 0.30, 200_000, self.clock)

    def test_detects_pump_and_dump_once(self):
        r = self.st.update([tk("AAAUSDT", 1, 2.5), tk("BBBUSDT", 1, 0.5), tk("CCCUSDT", 1, 1.1)])
        self.assertEqual(r["new_events"], 2)
        self.clock.t += 300_000
        self.assertEqual(self.st.update([tk("AAAUSDT", 1, 2.6)])["new_events"], 0)  # same day -> not duplicated
        kinds = {e["symbol"]: e["kind"] for e in self.st.summary()["events"]}
        self.assertEqual(kinds, {"AAAUSDT": "PUMP", "BBBUSDT": "DUMP"})

    def test_ignores_illiquid_and_bad_rows(self):
        r = self.st.update([tk("TINYUSDT", 1, 5, vol=1000), {"symbol": "X"}, {"lastPrice": "1"}, tk("ZUSDT", 0, 1)])
        self.assertEqual(r["new_events"], 0)

    def test_follows_price_and_horizons(self):
        self.st.update([tk("AAAUSDT", 1, 2.0)])
        t0 = self.clock.t
        for dt_h, price in ((1, 2.2), (4, 1.6), (24, 1.2)):
            self.clock.t = t0 + dt_h * H
            self.st.update([tk("AAAUSDT", 1, price)])
        e = self.st.summary()["events"][-1]  # the first (original) event; a new UTC day may add another
        self.assertEqual(e["after_1h_pct"], 10.0)
        self.assertEqual(e["after_4h_pct"], -20.0)
        self.assertEqual(e["after_24h_pct"], -40.0)
        self.assertEqual(e["best_since_pct"], 10.0)
        self.assertEqual(e["worst_since_pct"], -40.0)
        f = self.st.summary()["stats"]["PUMP"]["forward"]["after_24h_pct"]
        self.assertEqual(f["pct_kept_going"], 0.0)

    def test_no_trades_from_study(self):
        self.st.update([tk("AAAUSDT", 1, 3)])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM positions")["n"], 0)
        self.assertIn("no trades", self.st.summary()["mode"].lower())

    def test_scanner_runs_with_study_enabled(self):
        sc = Scanner(self.store, self.s, client=FakeClient(self.clock), clock=self.clock)
        self.assertIsNotNone(sc.study)
        r = sc.scan_once()
        self.assertEqual(r["status"], "ok", r["errors"])


if __name__ == "__main__":
    unittest.main()
