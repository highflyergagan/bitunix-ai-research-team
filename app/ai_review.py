"""AI second-opinion reviewers (Claude, optionally Grok) for trade candidates.

* Called ONLY for candidates that already passed the deterministic signal + risk pre-checks.
* Claude reviews first; Grok (if enabled) is asked only after Claude approves, so a trade
  needs BOTH to approve. Each provider has its own hard daily call and spend caps.
* Any error, timeout, malformed reply, or low confidence => REJECT (conservative).
* Reviewers can only approve or reject; they cannot change size, stop, or target.
* API keys are never logged, stored, or returned.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import requests

from .config import Settings
from .store import Store, now_ms

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
XAI_URL = "https://api.x.ai/v1/chat/completions"
API_URL = ANTHROPIC_URL  # backwards compatibility

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


def build_payload(signal: dict, account: dict, s: Settings) -> dict:
    return {
        "symbol": signal["symbol"], "proposed_bias": signal["bias"], "entry": signal["entry"],
        "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
        "deterministic_reasons": signal["reasons"], "indicators": signal["indicators"],
        "unavailable_data": signal["data_gaps"],
        "account": {k: account.get(k) for k in ("equity", "open_positions", "day_pnl", "drawdown_pct")},
        "risk_per_trade": f"{s.risk_per_trade:.2%} of equity",
    }


class _Reviewer:
    provider = ""

    def __init__(self, store: Store, settings: Settings, clock=now_ms, post=None):
        self.store = store
        self.s = settings
        self.clock = clock
        self._post = post or requests.post

    # --- provider-specific (override) ---
    mode: str
    model: str
    api_key: str
    max_calls: int
    max_usd: float
    min_conf: float
    in_price: float
    out_price: float
    key_name: str

    def _request(self, payload: dict) -> tuple[str, int, int, str]:
        raise NotImplementedError

    # --- shared ---
    def selftest(self) -> dict:
        """One tiny real API call to confirm the key and model work. Never returns the key."""
        if self.mode == "off":
            return {"provider": self.provider, "ok": None, "detail": "disabled"}
        if not self.api_key:
            return {"provider": self.provider, "ok": False, "detail": f"{self.key_name} missing"}
        try:
            text, inp, outp, model = self._request({"selftest": "Reply with the JSON object "
                                                    '{"decision":"REJECT","confidence":1,"reasons":["selftest"]}'})
            cost = inp / 1e6 * self.in_price + outp / 1e6 * self.out_price
            self.store.log("INFO", f"{self.provider} self-test OK (model {model}, ${cost:.5f})")
            return {"provider": self.provider, "ok": True, "model": model, "cost_usd": round(cost, 6),
                    "reply_parsed": bool(re.search(r"\{.*\}", text, re.S))}
        except Exception as exc:
            msg = type(exc).__name__ + ": " + str(exc)[:200]
            self.store.log("ERROR", f"{self.provider} self-test FAILED: {msg}")
            return {"provider": self.provider, "ok": False, "detail": msg}

    def usage_today(self) -> dict:
        t = self.clock()
        day0 = int(dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
                   .replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        r = self.store.one(
            "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost FROM ai_decisions "
            "WHERE ts>=? AND model IS NOT NULL AND provider=?", (day0, self.provider))
        return {"calls": r["calls"], "cost_usd": r["cost"]}

    def status(self) -> dict:
        u = self.usage_today()
        total = self.store.one("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) outp, "
                               "COALESCE(SUM(cost_usd),0) cost FROM ai_decisions WHERE model IS NOT NULL AND provider=?",
                               (self.provider,))
        return {"provider": self.provider, "mode": self.mode, "model": self.model, "configured": bool(self.api_key),
                "calls_today": u["calls"], "cost_today_usd": round(u["cost_usd"], 5),
                "max_calls_per_day": self.max_calls, "max_daily_usd": self.max_usd,
                "total_calls": total["calls"], "total_input_tokens": total["inp"],
                "total_output_tokens": total["outp"], "total_cost_usd": round(total["cost"], 5)}

    def _record(self, signal, signal_id, decision, confidence, reasons, model=None, inp=0, outp=0, cost=0.0,
                error=None) -> dict:
        cur = self.store.execute(
            "INSERT INTO ai_decisions(ts,symbol,signal_id,decision,confidence,reasons,model,input_tokens,output_tokens,"
            "cost_usd,error,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), signal["symbol"], signal_id, decision, confidence, json.dumps(reasons), model, inp, outp,
             cost, error, self.provider))
        return {"id": cur.lastrowid, "provider": self.provider, "decision": decision, "confidence": confidence,
                "reasons": reasons, "error": error, "cost_usd": cost}

    def review(self, signal: dict, account: dict, signal_id: int | None = None) -> dict:
        name = self.provider.capitalize()
        if self.mode == "off":
            return self._record(signal, signal_id, "APPROVE", None, [f"{name} review disabled; risk rules only"])
        if not self.api_key:
            if self.mode == "optional":
                return self._record(signal, signal_id, "APPROVE", None, [f"no {self.key_name}; {name} optional -> skipped"])
            return self._record(signal, signal_id, "REJECT", None, [f"{self.key_name} not configured ({name} required)"])
        u = self.usage_today()
        if u["calls"] >= self.max_calls or u["cost_usd"] >= self.max_usd:
            return self._record(signal, signal_id, "REJECT", None,
                                [f"daily {name} budget exhausted ({u['calls']} calls, ${u['cost_usd']:.4f})"])
        model, inp, outp, cost = self.model, 0, 0, 0.0
        try:
            text, inp, outp, model = self._request(build_payload(signal, account, self.s))
            cost = inp / 1e6 * self.in_price + outp / 1e6 * self.out_price
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError("no JSON in reply")
            out = json.loads(m.group(0))
            decision = str(out.get("decision", "")).upper()
            conf = float(out.get("confidence", 0))
            reasons = [str(x)[:300] for x in (out.get("reasons") or [])][:6]
            if decision not in ("APPROVE", "REJECT") or not 0 <= conf <= 1:
                raise ValueError("invalid decision format")
            if decision == "APPROVE" and conf < self.min_conf:
                reasons.append(f"confidence {conf:.2f} below threshold {self.min_conf} -> REJECT")
                decision = "REJECT"
            return self._record(signal, signal_id, decision, conf, reasons, model, inp, outp, cost)
        except Exception as exc:  # conservative: any failure rejects
            msg = type(exc).__name__ + ": " + str(exc)[:200]
            self.store.log("WARN", f"{name} review failed for {signal['symbol']}: {msg}")
            return self._record(signal, signal_id, "REJECT", None, [f"{name} review error -> conservative reject"],
                                model, inp, outp, cost, error=msg)


class ClaudeReviewer(_Reviewer):
    provider = "claude"
    key_name = "ANTHROPIC_API_KEY"

    def __init__(self, store, settings, clock=now_ms, post=None):
        super().__init__(store, settings, clock, post)
        s = settings
        self.mode, self.model, self.api_key = s.ai_mode, s.claude_model, s.anthropic_api_key
        self.max_calls, self.max_usd, self.min_conf = s.claude_max_calls_per_day, s.claude_max_daily_usd, s.claude_min_confidence
        self.in_price, self.out_price = s.claude_input_usd_per_mtok, s.claude_output_usd_per_mtok

    def _request(self, payload):
        body = {"model": self.model, "max_tokens": 300, "temperature": 0, "system": SYSTEM,
                "messages": [{"role": "user", "content": "Review this paper-trade candidate:\n" + json.dumps(payload)}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        r = self._post(ANTHROPIC_URL, headers=headers, json=body, timeout=self.s.claude_timeout_seconds)
        if r.status_code != 200:
            raise RuntimeError(f"Claude API HTTP {r.status_code}")
        j = r.json()
        usage = j.get("usage", {})
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), j.get("model", self.model)


class GrokReviewer(_Reviewer):
    provider = "grok"
    key_name = "XAI_API_KEY"

    def __init__(self, store, settings, clock=now_ms, post=None):
        super().__init__(store, settings, clock, post)
        s = settings
        self.mode, self.model, self.api_key = s.grok_mode, s.grok_model, s.xai_api_key
        if self.mode == "auto":
            self.mode = "required" if self.api_key else "off"
        self.max_calls, self.max_usd, self.min_conf = s.grok_max_calls_per_day, s.grok_max_daily_usd, s.grok_min_confidence
        self.in_price, self.out_price = s.grok_input_usd_per_mtok, s.grok_output_usd_per_mtok

    def _request(self, payload):
        body = {"model": self.model, "max_tokens": 300, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": "Review this paper-trade candidate:\n" + json.dumps(payload)}]}
        headers = {"Authorization": "Bearer " + self.api_key, "content-type": "application/json"}
        r = self._post(XAI_URL, headers=headers, json=body, timeout=self.s.claude_timeout_seconds)
        if r.status_code != 200:
            raise RuntimeError(f"Grok API HTTP {r.status_code}")
        j = r.json()
        usage = j.get("usage", {})
        choices = j.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), j.get("model", self.model)


class ReviewPanel:
    """Claude first; Grok only if Claude approved. Trade needs every enabled reviewer to approve."""

    def __init__(self, store: Store, settings: Settings, clock=now_ms, post=None):
        self.claude = ClaudeReviewer(store, settings, clock, post)
        self.grok = GrokReviewer(store, settings, clock, post)

    def review(self, signal: dict, account: dict, signal_id: int | None = None) -> dict:
        c = self.claude.review(signal, account, signal_id)
        if c["decision"] != "APPROVE":
            return {**c, "reasons": [f"Claude: {r}" for r in c["reasons"]]}
        g = self.grok.review(signal, account, signal_id)
        if g["decision"] != "APPROVE":
            return {**g, "reasons": [f"Grok: {r}" for r in g["reasons"]]}
        return {**c, "reasons": [f"Claude: {r}" for r in c["reasons"]] + [f"Grok: {r}" for r in g["reasons"]],
                "cost_usd": c["cost_usd"] + g["cost_usd"]}

    def selftest(self) -> dict:
        return {"claude": self.claude.selftest(), "grok": self.grok.selftest()}

    def status(self) -> dict:
        st = self.claude.status()
        st["grok"] = self.grok.status()
        return st
