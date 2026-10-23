"""Claude second-opinion reviewer for trade candidates.

* Called ONLY for candidates that already passed the deterministic signal + risk pre-checks.
* Hard daily call and spend caps.
* Any error, timeout, malformed reply, or low confidence => REJECT (conservative).
* Claude can only approve or reject; it cannot change size, stop, or target.
* The API key is never logged or returned.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import requests

from .config import Settings
from .store import Store, now_ms

API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = (
    "You are a cautious risk reviewer for a crypto futures PAPER-TRADING research system. "
    "You receive a deterministic technical signal computed from real Bitunix market data. "
    "Judge only from the data provided. Do not invent news, prices, or events. Missing data "
    "(news, token unlocks, listings, open interest) is genuinely unavailable, not neutral. "
    "Reject when the setup is weak, conflicting, overextended, or the data is insufficient. "
    "You cannot change position size, stop-loss, or take-profit. "
    'Reply with ONLY a JSON object: {"decision":"APPROVE"|"REJECT","confidence":0.0-1.0,'
    '"reasons":["short reason", "..."]}'
)


class ClaudeReviewer:
    def __init__(self, store: Store, settings: Settings, clock=now_ms, post=None):
        self.store = store
        self.s = settings
        self.clock = clock
        self._post = post or requests.post

    def usage_today(self) -> dict:
        t = self.clock()
        day0 = int(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
                   .replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        r = self.store.one(
            "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost FROM ai_decisions WHERE ts>=? AND model IS NOT NULL",
            (day0,))
        return {"calls": r["calls"], "cost_usd": r["cost"]}

    def status(self) -> dict:
        u = self.usage_today()
        total = self.store.one("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) outp, "
                               "COALESCE(SUM(cost_usd),0) cost FROM ai_decisions WHERE model IS NOT NULL")
        return {"mode": self.s.ai_mode, "model": self.s.claude_model, "configured": bool(self.s.anthropic_api_key),
                "calls_today": u["calls"], "cost_today_usd": round(u["cost_usd"], 5),
                "max_calls_per_day": self.s.claude_max_calls_per_day, "max_daily_usd": self.s.claude_max_daily_usd,
                "total_calls": total["calls"], "total_input_tokens": total["inp"],
                "total_output_tokens": total["outp"], "total_cost_usd": round(total["cost"], 5)}

    def _record(self, signal: dict, signal_id, decision, confidence, reasons, model=None, inp=0, outp=0, cost=0.0,
                error=None) -> dict:
        cur = self.store.execute(
            "INSERT INTO ai_decisions(ts,symbol,signal_id,decision,confidence,reasons,model,input_tokens,output_tokens,"
            "cost_usd,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), signal["symbol"], signal_id, decision, confidence, json.dumps(reasons), model, inp, outp,
             cost, error))
        return {"id": cur.lastrowid, "decision": decision, "confidence": confidence, "reasons": reasons,
                "error": error, "cost_usd": cost}

    def review(self, signal: dict, account: dict, signal_id: int | None = None) -> dict:
        mode = self.s.ai_mode
        if mode == "off":
            return self._record(signal, signal_id, "APPROVE", None, ["AI review disabled (AI_MODE=off); risk rules only"])
        if not self.s.anthropic_api_key:
            if mode == "optional":
                return self._record(signal, signal_id, "APPROVE", None, ["no API key; AI_MODE=optional -> risk rules only"])
            return self._record(signal, signal_id, "REJECT", None, ["ANTHROPIC_API_KEY not configured (AI_MODE=required)"])
        u = self.usage_today()
        if u["calls"] >= self.s.claude_max_calls_per_day or u["cost_usd"] >= self.s.claude_max_daily_usd:
            return self._record(signal, signal_id, "REJECT", None,
                                [f"daily Claude budget exhausted ({u['calls']} calls, ${u['cost_usd']:.4f})"])

        payload = {
            "symbol": signal["symbol"], "proposed_bias": signal["bias"], "entry": signal["entry"],
            "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
            "deterministic_reasons": signal["reasons"], "indicators": signal["indicators"],
            "unavailable_data": signal["data_gaps"],
            "account": {k: account.get(k) for k in ("equity", "open_positions", "day_pnl", "drawdown_pct")},
            "risk_per_trade": f"{self.s.risk_per_trade:.2%} of equity",
        }
        body = {
            "model": self.s.claude_model, "max_tokens": 300, "temperature": 0,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "Review this paper-trade candidate:\n" + json.dumps(payload)}],
        }
        headers = {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        model, inp, outp, cost = self.s.claude_model, 0, 0, 0.0
        try:
            r = self._post(API_URL, headers=headers, json=body, timeout=self.s.claude_timeout_seconds)
            if r.status_code != 200:
                # don't echo the response body: it could in theory include request metadata
                raise RuntimeError(f"Claude API HTTP {r.status_code}")
            j = r.json()
            usage = j.get("usage", {})
            inp, outp = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
            cost = inp / 1e6 * self.s.claude_input_usd_per_mtok + outp / 1e6 * self.s.claude_output_usd_per_mtok
            model = j.get("model", model)
            text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError("no JSON in reply")
            out = json.loads(m.group(0))
            decision = str(out.get("decision", "")).upper()
            conf = float(out.get("confidence", 0))
            reasons = [str(x)[:300] for x in (out.get("reasons") or [])][:6]
            if decision not in ("APPROVE", "REJECT") or not 0 <= conf <= 1:
                raise ValueError("invalid decision format")
            if decision == "APPROVE" and conf < self.s.claude_min_confidence:
                reasons.append(f"confidence {conf:.2f} below threshold {self.s.claude_min_confidence} -> REJECT")
                decision = "REJECT"
            return self._record(signal, signal_id, decision, conf, reasons, model, inp, outp, cost)
        except Exception as exc:  # conservative: any failure rejects
            msg = type(exc).__name__ + ": " + str(exc)[:200]
            self.store.log("WARN", f"Claude review failed for {signal['symbol']}: {msg}")
            return self._record(signal, signal_id, "REJECT", None, ["AI review error -> conservative reject"],
                                model, inp, outp, cost, error=msg)
