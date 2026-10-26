"""Real-trade journal: record only, derived entry, stats."""
import unittest

from app.journal import Journal
from tests.helpers import settings


class JournalTests(unittest.TestCase):
    def test_add_derives_entry_and_stats(self):
        j = Journal(settings().data_dir)
        r = j.add(symbol="capusdt", side="short", pnl=87.12683, fee=0.08723433, exit=0.07303, qty=2389, leverage=3)
        self.assertAlmostEqual(r["entry"], 0.07303 + 87.12683 / 2389, places=9)
        j.add(symbol="JCTUSDT", side="SHORT", pnl=-10, fee=0.02)
        st = j.summary()["stats"]
        self.assertEqual((st["count"], st["wins"], st["losses"]), (2, 1, 1))
        self.assertAlmostEqual(st["net_pnl"], 87.12683 - 0.08723433 - 10.02, places=4)
        self.assertGreater(st["profit_factor"], 8)

    def test_rejects_bad_input(self):
        j = Journal(settings().data_dir)
        with self.assertRaises(ValueError):
            j.add(symbol="CAP/USDT", side="SHORT", pnl=1)
        with self.assertRaises(ValueError):
            j.add(symbol="CAPUSDT", side="UP", pnl=1)


if __name__ == "__main__":
    unittest.main()
