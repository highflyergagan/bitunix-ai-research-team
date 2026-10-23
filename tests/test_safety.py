"""Safety guarantees: no live trading, no credentials, no order endpoints, risk limits cannot be loosened."""
import os
import pathlib
import re
import unittest
from unittest import mock

from app import bitunix, config
from app.config import LiveTradingRequestedError, Settings, assert_paper_only

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SafetyTests(unittest.TestCase):
    def test_live_trading_constants_off(self):
        self.assertFalse(config.LIVE_TRADING_ENABLED)
        self.assertTrue(config.PAPER_ONLY)

    def test_refuses_live_trading_env(self):
        for key in ("LIVE_TRADING", "LIVE_TRADING_ENABLED", "ENABLE_LIVE_TRADING", "REAL_TRADING"):
            with mock.patch.dict(os.environ, {key: "true"}):
                with self.assertRaises(LiveTradingRequestedError):
                    assert_paper_only()

    def test_refuses_exchange_credentials(self):
        with mock.patch.dict(os.environ, {"BITUNIX_API_KEY": "x"}):
            with self.assertRaises(LiveTradingRequestedError):
                assert_paper_only()

    def test_client_only_allows_public_market_paths(self):
        c = bitunix.BitunixPublicClient(pause=0)
        for path in ("/api/v1/futures/trade/place_order", "/api/v1/futures/account", "/api/v1/futures/trade/cancel_orders"):
            with self.assertRaises(PermissionError):
                c._get(path)
        self.assertTrue(all("/market/" in p for p in bitunix.ALLOWED_PATHS))

    def test_no_order_or_withdraw_code_in_app(self):
        pattern = re.compile(r"place_order|placeOrder|/trade/|/withdraw|withdraw\(|X-SIGN|import hmac", re.I)
        for f in (ROOT / "app").rglob("*.py"):
            text = f.read_text()
            self.assertIsNone(pattern.search(text), f"suspicious order/auth code in {f.name}")

    def test_client_uses_get_only(self):
        src = (ROOT / "app" / "bitunix.py").read_text()
        self.assertNotRegex(src, r"\.(post|put|delete|patch)\(")

    def test_env_cannot_loosen_risk_limits(self):
        env = {"RISK_PER_TRADE": "0.05", "MAX_DAILY_LOSS": "0.5", "MAX_DRAWDOWN": "0.9", "MAX_OPEN_POSITIONS": "10",
               "MAX_LEVERAGE": "50"}
        with mock.patch.dict(os.environ, env):
            s = Settings.from_env()
        self.assertEqual(s.risk_per_trade, 0.005)
        self.assertEqual(s.max_daily_loss, 0.02)
        self.assertEqual(s.max_drawdown, 0.05)
        self.assertEqual(s.max_open_positions, 2)
        self.assertEqual(s.max_leverage, 3.0)

    def test_env_can_tighten_risk_limits(self):
        with mock.patch.dict(os.environ, {"RISK_PER_TRADE": "0.0025"}):
            self.assertEqual(Settings.from_env().risk_per_trade, 0.0025)

    def test_secrets_not_in_repr(self):
        s = Settings()
        s.anthropic_api_key = "sk-ant-SECRET"
        s.dashboard_token = "TOKENSECRET"
        self.assertNotIn("SECRET", repr(s))


if __name__ == "__main__":
    unittest.main()
