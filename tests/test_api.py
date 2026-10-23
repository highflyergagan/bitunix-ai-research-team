"""Dashboard API: authentication and controls (uses FastAPI's TestClient, which needs httpx)."""
import os
import unittest

try:
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_CLIENT = True
except Exception:  # pragma: no cover
    HAVE_CLIENT = False

from tests.helpers import Clock, FakeClient, settings

TOKEN = "test-token-0123456789abcdef"


@unittest.skipUnless(HAVE_CLIENT, "fastapi TestClient/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("DATA_DIR", settings().data_dir)
        from app.main import create_app
        self.s = settings(dashboard_token=TOKEN)
        self.app = create_app(self.s, client=FakeClient(Clock()), start_scheduler=False)
        self.c = TestClient(self.app)
        self.h = {"Authorization": "Bearer " + TOKEN}

    def tearDown(self):
        self.app.state.scanner.stop()

    def test_health_public_and_paper_only(self):
        j = self.c.get("/health").json()
        self.assertEqual(j["trading"], "paper-only")
        self.assertFalse(j["live_trading_enabled"])

    def test_private_endpoints_require_token(self):
        for path in ("/api/status", "/api/positions", "/api/signals", "/api/ai-decisions", "/api/report", "/api/scan"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
            self.assertEqual(self.c.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401, path)
        for path in ("/api/automation/start", "/api/scan-now", "/api/account/reset"):
            self.assertEqual(self.c.post(path).status_code, 401, path)

    def test_missing_token_config_locks_api(self):
        from app.main import create_app
        app = create_app(settings(dashboard_token=""), client=FakeClient(Clock()), start_scheduler=False)
        self.assertEqual(TestClient(app).get("/api/status", headers={"Authorization": "Bearer "}).status_code, 503)

    def test_status_and_scan_now(self):
        self.assertEqual(self.c.get("/api/status", headers=self.h).json()["account"]["equity"], 100.0)
        r = self.c.post("/api/scan-now", headers=self.h).json()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(self.c.get("/api/signals", headers=self.h).json()["signals"])

    def test_reset_requires_confirmation(self):
        self.assertEqual(self.c.post("/api/account/reset", headers=self.h, json={}).status_code, 400)
        r = self.c.post("/api/account/reset", headers=self.h, json={"confirm": "RESET PAPER ACCOUNT"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
