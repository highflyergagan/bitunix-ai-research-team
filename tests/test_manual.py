"""Manual paper desk: own ledger, risk engine applies, exits from candles."""
import unittest

from app.manual import ManualDesk
from app.risk import RiskRejected
from app.store import Store
from tests.helpers import Clock, FakeClient, settings


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.clock = Clock()
        self.fc = FakeClient(self.clock)
        self.desk = ManualDesk(self.s, self.fc, lambda *a: [], self.clock)
        self.px = float([x for x in self.fc.tickers() if x["symbol"] == "SOLUSDT"][0]["lastPrice"])

    def test_open_short_in_own_ledger(self):
        p = self.desk.open("solusdt", "short", self.px * 1.06, self.px * 0.90)
        self.assertEqual((p["symbol"], p["side"]), ("SOLUSDT", "SHORT"))
        self.assertLessEqual(p["risk_usd"], 0.5 + 1e-9)
        self.assertEqual(Store(self.s.db_path).query("SELECT * FROM positions"), [])  # main ledger untouched

    def test_risk_engine_rejects_bad_levels(self):
        with self.assertRaises(RiskRejected):
            self.desk.open("SOLUSDT", "SHORT", self.px * 0.95, self.px * 0.90)  # stop below entry
        with self.assertRaises(RiskRejected):
            self.desk.open("SOLUSDT", "SHORT", self.px * 1.06, self.px * 0.99)  # reward:risk too low
        with self.assertRaises(RiskRejected):
            self.desk.open("NOPEUSDT", "SHORT", 2, 1)


if __name__ == "__main__":
    unittest.main()
