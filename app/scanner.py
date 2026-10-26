"""5-minute paper-trading automation loop.

One scan:
  1. update open paper positions from real 1m candles (stops / targets / funding / time stop)
  2. roll day + enforce kill switches
  3. build universe (favorites + top-N liquid USDT pairs) and compute signals
  4. for LONG/SHORT candidates: risk pre-check -> Claude review -> final risk check -> open paper position
Overlap is prevented with a non-blocking lock; one process only (run uvicorn with --workers 1).
"""
from __future__ import annotations

import json
import logging
import threading
import traceback

from .ai_review import ReviewPanel
from .committee import ResearchCommittee
from .bitunix import INTERVAL_MS, BitunixPublicClient, MarketDataError
from .config import Settings
from .paper import PaperBroker
from .risk import RiskRejected
from .store import Store, now_ms
from .strategy import LONG, SHORT, generate_signal
from .study import StudyMode
from .lab import StrategyLab
from .pump_agent import PumpExhaustionAgent
from .manual import ManualDesk

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, store: Store, settings: Settings, client=None, broker=None, reviewer=None, clock=now_ms):
        self.store = store
        self.s = settings
        self.clock = clock
        self.client = client or BitunixPublicClient(pause=settings.request_pause_seconds)
        self.broker = broker or PaperBroker(store, settings, clock)
        self.reviewer = reviewer or (ResearchCommittee(store, settings, clock) if settings.review_mode == "committee"
                                     else ReviewPanel(store, settings, clock))
        self.lab = (StrategyLab(settings, self.client, self._candles_since, clock, settings.lab_pump_threshold)
                    if settings.lab_enabled else None)
        self.pump_agent = (PumpExhaustionAgent(settings, self.client, self.reviewer, self._candles_since, store, clock)
                           if settings.pump_agent_enabled else None)
        self.manual = ManualDesk(settings, self.client, self._candles_since, clock)
        self.study = StudyMode(store, settings.study_min_change, settings.study_min_volume, clock) if settings.study_enabled else None
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.next_scan_at: int | None = None
        self.suspended: dict[str, int] = {}  # symbol -> until (ms); Bitunix error 20015 = contract not tradable

    # ---------- automation control ----------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> bool:
        self.store.set("automation_enabled", True)
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="paper-scanner", daemon=True)
        self._thread.start()
        self.store.log("INFO", "Paper automation started")
        return True

    def stop(self) -> None:
        self.store.set("automation_enabled", False)
        self._stop.set()
        self.next_scan_at = None
        self.store.log("INFO", "Paper automation stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.scan_once()
            except Exception:  # never let the loop die
                self.store.log("ERROR", "Unhandled scan error: " + traceback.format_exc()[-1500:])
            interval_ms = self.s.scan_interval_seconds * 1000
            # align to the interval grid (e.g. :00, :05, :10) plus a few seconds for candle close
            nxt = (started // interval_ms + 1) * interval_ms + 5_000
            self.next_scan_at = nxt
            self._stop.wait(max(1.0, (nxt - self.clock()) / 1000))

    # ---------- scanning ----------
    def scan_once(self) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "a scan is already running"}
        try:
            return self._scan()
        finally:
            self._scan_lock.release()

    def _scan(self) -> dict:
        t0 = self.clock()
        req0 = getattr(self.client, "request_count", 0)
        scan_id = self.store.execute("INSERT INTO scans(started_at,status) VALUES(?, 'running')", (t0,)).lastrowid
        stats = {"symbols_scanned": 0, "signals": 0, "candidates": 0, "opened": 0, "closed": 0}
        errors: list[str] = []

        # 1. manage open positions first (exits must never be blocked by entry logic)
        for p in self.broker.open_positions():
            try:
                candles = self._candles_since(p["symbol"], p["last_checked_ms"])
                fr = self.client.funding_rate(p["symbol"])
                after = self.broker.process_candles(p, candles, INTERVAL_MS["1m"], fr)
                if after["status"] == "CLOSED":
                    stats["closed"] += 1
            except Exception as exc:
                errors.append(f"{p['symbol']} position update: {exc}")

        acct = self.broker.roll_day_and_limits()

        # 2. universe
        try:
            tick = self.client.tickers()
        except MarketDataError as exc:
            errors.append(f"tickers: {exc}")
            return self._finish(scan_id, t0, req0, stats, errors, "failed")
        # study mode (research only, never trades) -- must never break the trading scan
        if self.study:
            try:
                stats["study"] = self.study.update(tick)
            except Exception as exc:
                errors.append(f"study mode: {exc}")
        by_sym = {}
        for x in tick:
            try:
                by_sym[x["symbol"]] = {"price": float(x.get("lastPrice") or x.get("last") or x.get("markPrice") or 0),
                                       "quote_volume": float(x.get("quoteVol") or 0)}
            except (KeyError, TypeError, ValueError):
                continue
        now0 = self.clock()
        self.suspended = {k: v for k, v in self.suspended.items() if v > now0}
        ranked = sorted((s for s in by_sym if s.endswith("USDT") and by_sym[s]["price"] > 0 and s not in self.suspended),
                        key=lambda s: by_sym[s]["quote_volume"], reverse=True)
        universe = list(dict.fromkeys([f for f in self.s.favorites if f in by_sym and f not in self.suspended] + ranked[: self.s.universe_top_n]))

        # update marks for open positions from tickers
        for p in self.broker.open_positions():
            if p["symbol"] in by_sym and by_sym[p["symbol"]]["price"] > 0:
                self.store.execute("UPDATE positions SET mark_price=? WHERE id=?", (by_sym[p["symbol"]]["price"], p["id"]))

        # 3. signals
        candidates = []
        market: dict[str, dict] = {}
        for sym in universe:
            try:
                c15 = self.client.klines(sym, "15m", 200)
                c1h = self.client.klines(sym, "1h", 200)
                now = self.clock()
                c15 = [c for c in c15 if c["time"] + INTERVAL_MS["15m"] <= now]  # closed candles only
                c1h = [c for c in c1h if c["time"] + INTERVAL_MS["1h"] <= now]
                fr = self.client.funding_rate(sym)
                sig = generate_signal(sym, c15, c1h, quote_volume_24h=by_sym[sym]["quote_volume"], funding_rate=fr,
                                      min_quote_volume=self.s.min_quote_volume,
                                      require_verified_event_feeds=self.s.require_verified_event_feeds)
                stats["symbols_scanned"] += 1
                market[sym] = {"c15": c15, "c1h": c1h, "funding_rate": fr, "main_signal": sig,
                               "quote_volume": by_sym[sym]["quote_volume"]}
                sid = self.store.execute(
                    "INSERT INTO signals(scan_id,ts,symbol,bias,score,entry,stop_loss,take_profit,payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    (scan_id, now, sym, sig["bias"], sig["score"], sig["entry"], sig["stop_loss"], sig["take_profit"],
                     json.dumps(sig))).lastrowid
                if sig["bias"] in (LONG, SHORT):
                    stats["signals"] += 1
                    candidates.append((sig, sid))
            except Exception as exc:
                if "20015" in str(exc):  # suspended contract: skip it for 24h instead of failing every scan
                    self.suspended[sym] = self.clock() + 86_400_000
                    self.store.log("INFO", f"{sym} is not tradable on Bitunix (error 20015); skipped for 24h")
                else:
                    errors.append(f"{sym}: {exc}")

        # 4. candidates -> risk -> Claude -> risk -> paper entry
        candidates.sort(key=lambda x: x[0]["score"], reverse=True)
        for sig, sid in candidates:
            side = "LONG" if sig["bias"] == LONG else "SHORT"
            pre = self.broker.check(sig["symbol"], side)
            if pre:
                self.store.log("INFO", f"{sig['symbol']} {side} skipped before AI review: {'; '.join(pre)}")
                continue
            stats["candidates"] += 1
            decision = self.reviewer.review(sig, self.broker.account(), sid)
            if decision["decision"] != "APPROVE":
                self.store.log("INFO", f"{sig['symbol']} {side} rejected by AI review: {'; '.join(decision['reasons'])}")
                continue
            try:
                price = by_sym.get(sig["symbol"], {}).get("price") or sig["entry"]
                self.broker.open_position(sig, signal_id=sid, ai_decision_id=decision["id"], market_price=price)
                stats["opened"] += 1
            except RiskRejected as exc:
                self.store.log("INFO", f"{sig['symbol']} {side} rejected by risk engine: {exc}")
            except Exception as exc:
                errors.append(f"{sig['symbol']} open: {exc}")

        # 5. strategy lab (separate paper accounts, rule-only) -- must never break the main scan
        if self.lab:
            try:
                prices = {k: v["price"] for k, v in by_sym.items() if v["price"] > 0}
                lab = self.lab.run(market, prices, tick)
                stats["lab"] = {k: {"opened": v["opened"], "closed": v["closed"]} for k, v in lab.items()}
                for k, v in lab.items():
                    if v["opened"] or v["closed"]:
                        self.store.log("LAB", f"{k}: opened {v['opened']}, closed {v['closed']} (paper)")
            except Exception as exc:
                errors.append(f"strategy lab: {exc}")

        # 6. 100% Pump Exhaustion SHORT Agent (own paper ledger) -- must never break the main scan
        if self.pump_agent:
            try:
                prices = {k: v["price"] for k, v in by_sym.items() if v["price"] > 0}
                pa = self.pump_agent.run(tick, prices)
                stats["pump_agent"] = {k: pa[k] for k in ("evaluated", "signals", "opened", "closed")}
                if pa["errors"]:
                    errors.append("pump agent: " + "; ".join(pa["errors"])[:300])
            except Exception as exc:
                errors.append(f"pump agent: {exc}")

        # 7. manual paper desk (user's own ideas, own ledger): exits only
        try:
            prices = {k: v["price"] for k, v in by_sym.items() if v["price"] > 0}
            stats["manual_closed"] = self.manual.manage(prices)
        except Exception as exc:
            errors.append(f"manual desk: {exc}")

        self.broker.roll_day_and_limits()
        self.broker.record_equity()
        status = "ok" if not errors else ("partial" if stats["symbols_scanned"] else "failed")
        return self._finish(scan_id, t0, req0, stats, errors, status)

    def _candles_since(self, symbol: str, since_ms: int) -> list[dict]:
        """1m candles from since_ms to now, paging forward (max ~33h)."""
        out: list[dict] = []
        start = since_ms - 60_000
        for _ in range(10):
            try:
                batch = self.client.klines(symbol, "1m", 200, start_ms=start)
            except MarketDataError:
                break
            batch = [c for c in batch if c["time"] >= start]
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 200:
                break
            start = batch[-1]["time"] + 60_000
        if not out:  # API may not support startTime -- fall back to the latest candles
            out = self.client.klines(symbol, "1m", 200)
        dedup = {c["time"]: c for c in out}
        return [dedup[k] for k in sorted(dedup)]

    def _finish(self, scan_id, t0, req0, stats, errors, status) -> dict:
        t1 = self.clock()
        reqs = getattr(self.client, "request_count", 0) - req0
        err = "\n".join(errors)[:4000] or None
        self.store.execute(
            "UPDATE scans SET finished_at=?, status=?, symbols_scanned=?, signals=?, candidates=?, opened=?, closed=?, "
            "api_requests=?, error=? WHERE id=?",
            (t1, status, stats["symbols_scanned"], stats["signals"], stats["candidates"], stats["opened"],
             stats["closed"], reqs, err, scan_id))
        if status in ("ok", "partial"):
            self.store.set("last_successful_scan", t1)
        if errors:
            self.store.log("WARN" if status == "partial" else "ERROR", f"Scan {scan_id} {status}: " + "; ".join(errors)[:1500])
        return {"scan_id": scan_id, "status": status, "duration_s": round((t1 - t0) / 1000, 1), **stats,
                "errors": errors}
