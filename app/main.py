"""FastAPI app: Bitunix AI research desk + $100 PAPER-TRADING automation.

PAPER ONLY. No Bitunix credentials, no order endpoints, no live trading.
Run with a single worker:  uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from .config import LIVE_TRADING_ENABLED, Settings, assert_paper_only, load_dotenv
from .core import DEPARTMENTS, FAVORITES, candles, paper_trade, rank, tickers
from .report import build_report, start_experiment
from .scanner import Scanner
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
DASHBOARD_HTML = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")


def create_app(settings: Settings | None = None, client=None, start_scheduler: bool = True) -> FastAPI:
    if settings is None:
        load_dotenv()
    assert_paper_only()
    s = settings or Settings.from_env()
    store = Store(s.db_path)
    scanner = Scanner(store, s, client=client)

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler and (s.autostart or store.get("automation_enabled", False)):
            scanner.start()
        yield
        scanner._stop.set()

    app = FastAPI(title="Bitunix Research Desk — PAPER ONLY", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.store, app.state.scanner = s, store, scanner

    def auth(authorization: str | None = Header(None)):
        token = s.dashboard_token
        if not token or len(token) < 16:
            raise HTTPException(503, "DASHBOARD_TOKEN is not configured (min 16 characters)")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "Token required")

    def rows(sql, params=()):
        return store.query(sql, params)

    # ---------- public ----------
    @app.get("/", response_class=HTMLResponse)
    def home():
        with open(DASHBOARD_HTML, encoding="utf-8") as f:
            return f.read()

    @app.get("/health")
    def health():
        return {"status": "ok", "trading": "paper-only", "live_trading_enabled": LIVE_TRADING_ENABLED,
                "automation_running": scanner.running}

    # ---------- dashboard data (authenticated) ----------
    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        last = store.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        return {
            "automation_running": scanner.running,
            "automation_enabled": store.get("automation_enabled", False),
            "scan_interval_seconds": s.scan_interval_seconds,
            "next_scan_at": scanner.next_scan_at,
            "last_scan": last,
            "last_successful_scan": store.get("last_successful_scan"),
            "total_scans": store.one("SELECT COUNT(*) n FROM scans")["n"],
            "account": scanner.broker.account(),
            "risk": {
                "risk_per_trade_pct": s.risk_per_trade * 100, "max_daily_loss_pct": s.max_daily_loss * 100,
                "max_drawdown_pct": s.max_drawdown * 100, "max_open_positions": s.max_open_positions,
                "max_leverage": s.max_leverage, "max_trades_per_day": s.max_trades_per_day,
                "taker_fee_pct": s.taker_fee * 100, "slippage_pct": s.slippage * 100,
                "min_reward_risk_after_costs": s.min_reward_risk, "max_hold_hours": s.max_hold_hours,
                "ai_reviewers": "Claude" + (" + Grok (both must approve)" if scanner.reviewer.grok.mode != "off" else ""),
            },
            "claude": scanner.reviewer.status(),
            "experiment": store.get("experiment"),
            "universe": {"favorites": s.favorites, "top_n_by_volume": s.universe_top_n,
                         "min_quote_volume": s.min_quote_volume},
        }

    @app.get("/api/positions", dependencies=[Depends(auth)])
    def positions():
        return {"open": scanner.broker.open_positions(), "closed": scanner.broker.closed_positions(100)}

    @app.get("/api/signals", dependencies=[Depends(auth)])
    def signals(limit: int = 60, actionable_only: bool = False):
        where = "WHERE bias!='NO TRADE'" if actionable_only else ""
        out = rows(f"SELECT * FROM signals {where} ORDER BY id DESC LIMIT ?", (min(limit, 500),))
        for r in out:
            r["payload"] = json.loads(r["payload"])
        return {"signals": out}

    @app.get("/api/ai-decisions", dependencies=[Depends(auth)])
    def ai_decisions(limit: int = 50):
        out = rows("SELECT * FROM ai_decisions ORDER BY id DESC LIMIT ?", (min(limit, 500),))
        for r in out:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return {"decisions": out, "usage": scanner.reviewer.status()}

    @app.get("/api/scans", dependencies=[Depends(auth)])
    def scans(limit: int = 30):
        return {"scans": rows("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (min(limit, 500),))}

    @app.get("/api/events", dependencies=[Depends(auth)])
    def events(limit: int = 60):
        return {"events": rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 500),))}

    @app.get("/api/equity", dependencies=[Depends(auth)])
    def equity(limit: int = 600):
        return {"equity": list(reversed(rows("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (min(limit, 5000),))))}

    @app.get("/api/report", dependencies=[Depends(auth)])
    def report():
        return build_report(store, s)

    # ---------- controls (authenticated) ----------
    @app.post("/api/automation/start", dependencies=[Depends(auth)])
    def automation_start():
        scanner.start()
        return {"automation_running": True}

    @app.post("/api/automation/stop", dependencies=[Depends(auth)])
    def automation_stop():
        scanner.stop()
        return {"automation_running": False}

    @app.post("/api/scan-now", dependencies=[Depends(auth)])
    def scan_now():
        return scanner.scan_once()

    @app.post("/api/ai/selftest", dependencies=[Depends(auth)])
    def ai_selftest():
        return scanner.reviewer.selftest()

    @app.post("/api/experiment/start", dependencies=[Depends(auth)])
    def experiment_start(hours: float = 24.0):
        exp = start_experiment(store, scanner.broker.account()["equity"], max(0.1, min(hours, 168)))
        scanner.start()
        return {"experiment": exp, "automation_running": True}

    @app.post("/api/account/reset", dependencies=[Depends(auth)])
    def account_reset(confirm: str = Body("", embed=True)):
        if confirm != "RESET PAPER ACCOUNT":
            raise HTTPException(400, 'Send {"confirm": "RESET PAPER ACCOUNT"}')
        scanner.stop()
        scanner.broker.reset()
        store.set("experiment", None)
        return {"account": scanner.broker.account(), "automation_running": False}

    # ---------- original research endpoints (kept) ----------
    @app.get("/api/favorites", dependencies=[Depends(auth)])
    def favorites():
        return {"favorites": FAVORITES, "departments": DEPARTMENTS}

    @app.get("/api/scan", dependencies=[Depends(auth)])
    def market_scan():
        try:
            return {"symbols": rank(tickers()), "source": "Bitunix public futures tickers"}
        except Exception as e:
            raise HTTPException(503, str(e))

    @app.get("/api/research/{symbol}", dependencies=[Depends(auth)])
    def research(symbol: str):
        symbol = symbol.upper()
        if not symbol.isalnum() or len(symbol) > 24:
            raise HTTPException(400, "Invalid symbol")
        try:
            from .graph import make_graph
            return make_graph().invoke({"symbol": symbol, "candles": candles(symbol), "external": {},
                                        "reports": {}, "result": {}})["result"]
        except Exception as e:
            raise HTTPException(503, str(e))

    @app.get("/api/paper-example", dependencies=[Depends(auth)])
    def example():
        return paper_trade(100, 100, 98)

    return app


app = create_app()
