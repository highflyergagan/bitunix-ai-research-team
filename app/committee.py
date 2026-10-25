"""AI Research Committee -- the org-chart structure:

    Chief Market Intelligence Officer
    ├── Market News Research Lead            (Research, Sentiment, Stock Movement, News Credibility, Market Impact)
    ├── Equity Research Lead                 (Fundamental Research, Financial Forensics, Pre-Earnings Intelligence)
    ├── Head of Industry & Geopolitical      (Industry Research, Geopolitical Risk)
    ├── Head of Market Activity & Ownership  (Market Volume Intelligence, Insider Activity, Fund Holdings, Short-Seller)
    └── Independent Research Review Lead     (Bull Case, Bear Case, Research Validation)  -- run by Grok

Honesty rules:
* An analyst whose data source does not exist (news, insider wallets, fund holdings, earnings...) reports
  stance "no_data" with the reason. It is never asked to guess, and no AI call is spent on it.
* A department whose analysts all lack data is filled in deterministically (no AI call).
* Only runs for candidates that already passed the deterministic signal + risk pre-checks.
* Trade needs: Independent Review verdict APPROVE (>= min confidence) AND Chief APPROVE (>= min confidence).
* Any error, timeout or malformed reply => REJECT. Committee cannot change size, stop or target.
"""
from __future__ import annotations

import datetime as dt
import json

from .ai_review import REVIEW_CACHE_MINUTES, ClaudeReviewer, GrokReviewer
from .config import Settings
from .store import Store, now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS committee_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, symbol TEXT NOT NULL, bias TEXT NOT NULL,
  signal_id INTEGER, decision TEXT NOT NULL, confidence REAL, summary TEXT, reports TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0, ai_calls INTEGER NOT NULL DEFAULT 0, error TEXT
);
"""

NO_DATA = {
    "News Credibility Analyst": "no verified crypto news feed is connected",
    "Market Impact Analyst": "no news/event feed to measure impact from",
    "Fundamental Research Analyst": "no tokenomics / project fundamentals data source",
    "Financial Forensics Analyst": "no on-chain treasury or project financials data source",
    "Pre-Earnings Intelligence Analyst": "crypto has no earnings; no token-unlock / event calendar connected",
    "Industry Research Analyst": "no sector / industry data source",
    "Geopolitical Risk Analyst": "no geopolitical / regulatory news feed",
    "Insider Activity Analyst": "no whale / insider wallet tracking connected",
    "Fund Holdings Research Analyst": "no ETF / institutional holdings data source",
}

DEPARTMENTS = [
    {"key": "news", "lead": "Market News Research Lead", "color": "red", "provider": "claude",
     "analysts": [
         ("Research Analyst", "Summarise what the available market data says about this coin right now."),
         ("Sentiment Analyst", "Crowd positioning from the funding rate (positive = longs pay shorts = crowded long)."),
         ("Stock Movement Analyst", "Price action: trend on 1h and 15m, momentum, RSI, distance to support/resistance."),
         ("News Credibility Analyst", None),
         ("Market Impact Analyst", None)]},
    {"key": "equity", "lead": "Equity Research Lead", "color": "blue", "provider": None,
     "analysts": [("Fundamental Research Analyst", None), ("Financial Forensics Analyst", None),
                  ("Pre-Earnings Intelligence Analyst", None)]},
    {"key": "industry", "lead": "Head of Industry & Geopolitical Research", "color": "orange", "provider": None,
     "analysts": [("Industry Research Analyst", None), ("Geopolitical Risk Analyst", None)]},
    {"key": "activity", "lead": "Head of Market Activity & Ownership Research", "color": "green", "provider": "claude",
     "analysts": [
         ("Market Volume Intelligence Analyst", "24h quote volume, liquidity, and whether recent volume is expanding."),
         ("Insider Activity Analyst", None), ("Fund Holdings Research Analyst", None),
         ("Short-Seller Research Analyst", "Short-side positioning from the funding rate; are shorts or longs crowded?")]},
    {"key": "review", "lead": "Independent Research Review Lead", "color": "white", "provider": "grok",
     "analysts": [
         ("Bull Case Analyst", "The strongest honest case FOR the proposed trade, using only the data given."),
         ("Bear Case Analyst", "The strongest honest case AGAINST the proposed trade, using only the data given."),
         ("Research Validation Analyst", "Check the other departments' findings for errors, contradictions, or claims "
                                         "not supported by the data. Flag anything invented.")]},
]

BASE_SYSTEM = (
    "You are part of an AI research committee for a crypto futures PAPER-TRADING system (no real money). "
    "You only see real Bitunix market data that is provided. Never invent news, prices, events, wallets or numbers. "
    "If the data does not support a conclusion, say so. Keep each finding under 45 words."
)

ANALYST_ITEM = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "finding": {"type": "string"},
        "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["role", "finding", "stance", "confidence"],
}
DEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "analysts": {"type": "array", "items": ANALYST_ITEM},
        "lead_summary": {"type": "string"},
        "lead_stance": {"type": "string", "enum": ["supports_trade", "against_trade", "mixed"]},
    },
    "required": ["analysts", "lead_summary", "lead_stance"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {**DEPT_SCHEMA["properties"],
                   "verdict": {"type": "string", "enum": ["APPROVE", "REJECT"]},
                   "verdict_confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": DEPT_SCHEMA["required"] + ["verdict", "verdict_confidence"],
}
CHIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REJECT"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["decision", "confidence", "summary", "reasons"],
}


def _market_data(signal: dict) -> dict:
    extra = {"strategy_context": signal["review_context"]} if signal.get("review_context") else {}
    return {**extra, "symbol": signal["symbol"], "proposed_trade": signal["bias"], "entry": signal["entry"],
            "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
            "rule_engine_reasons": signal["reasons"], "indicators": signal["indicators"],
            "unavailable_data": signal["data_gaps"]}


class ResearchCommittee:
    def __init__(self, store: Store, settings: Settings, clock=now_ms, post=None):
        self.store = store
        self.s = settings
        self.clock = clock
        self.claude = ClaudeReviewer(store, settings, clock, post)
        self.grok = GrokReviewer(store, settings, clock, post)
        self.store.conn.executescript(SCHEMA)

    # ---------- helpers ----------
    def _engine(self, provider: str):
        if provider == "grok" and self.grok.mode != "off":
            return self.grok
        return self.claude  # Claude stands in for Grok when Grok is disabled

    def _log_call(self, eng, signal, signal_id, decision, conf, reasons, res=None, error=None):
        return eng._record(signal, signal_id, decision, conf, reasons,
                           (res or {}).get("model", eng.model), (res or {}).get("input_tokens", 0),
                           (res or {}).get("output_tokens", 0), (res or {}).get("cost_usd", 0.0), error)

    @staticmethod
    def _no_data_dept(d: dict) -> dict:
        return {"key": d["key"], "lead": d["lead"], "color": d["color"], "engine": "rules (no AI call)",
                "lead_summary": "No connected data source for this department; it abstains.",
                "lead_stance": "no_data",
                "analysts": [{"role": r, "finding": NO_DATA.get(r, "no data source"), "stance": "no_data",
                              "confidence": None} for r, _ in d["analysts"]]}

    def _ask_department(self, d, signal, signal_id, context: dict, schema: dict) -> tuple[dict, dict]:
        eng = self._engine(d["provider"])
        live = [(r, task) for r, task in d["analysts"] if task]
        prompt = {
            "you_are": d["lead"], "your_analysts": [{"role": r, "task": t} for r, t in live],
            "instructions": "Write one finding per listed analyst (same role names), then your lead summary.",
            **context,
        }
        res = eng.call_json(BASE_SYSTEM, json.dumps(prompt), schema)
        out = res["data"]
        got = {a.get("role"): a for a in out.get("analysts", []) if isinstance(a, dict)}
        analysts = []
        for r, task in d["analysts"]:
            if not task:
                analysts.append({"role": r, "finding": NO_DATA.get(r, "no data source"), "stance": "no_data",
                                 "confidence": None})
                continue
            a = got.get(r) or {}
            stance = a.get("stance") if a.get("stance") in ("bullish", "bearish", "neutral") else "neutral"
            try:
                conf = max(0.0, min(1.0, float(a.get("confidence", 0))))
            except (TypeError, ValueError):
                conf = 0.0
            analysts.append({"role": r, "finding": str(a.get("finding", "no answer"))[:400], "stance": stance,
                             "confidence": round(conf, 2)})
        rep = {"key": d["key"], "lead": d["lead"], "color": d["color"],
               "engine": f"{eng.provider} ({res['model']})", "lead_summary": str(out.get("lead_summary", ""))[:500],
               "lead_stance": out.get("lead_stance", "mixed"), "analysts": analysts}
        return rep, res

    # ---------- main ----------
    def review(self, signal: dict, account: dict, signal_id: int | None = None) -> dict:
        t = self.clock()
        if self.claude.mode == "off":
            return self.claude._record(signal, signal_id, "APPROVE", None, ["AI review disabled; risk rules only"])
        # same rejected setup within the cache window -> don't convene again
        prev = self.store.one(
            "SELECT ts, confidence FROM committee_runs WHERE symbol=? AND bias=? AND decision='REJECT' AND error IS NULL "
            "AND ts>=? ORDER BY ts DESC LIMIT 1", (signal["symbol"], signal["bias"], t - REVIEW_CACHE_MINUTES * 60_000))
        if prev:
            when = dt.datetime.fromtimestamp(prev["ts"] / 1000).strftime("%H:%M")
            return self.claude._record(signal, signal_id, "REJECT", prev["confidence"],
                                       [f"Committee already rejected this {signal['bias']} setup at {when}; "
                                        f"not reconvening for {REVIEW_CACHE_MINUTES} min"])
        for eng in {self.claude, self._engine("grok")}:
            if not eng.api_key:
                return eng._record(signal, signal_id, "REJECT", None, [f"{eng.key_name} not configured"])
            u = eng.usage_today()
            if u["calls"] >= eng.max_calls or u["cost_usd"] >= eng.max_usd:
                return eng._record(signal, signal_id, "REJECT", None,
                                   [f"daily {eng.provider} budget exhausted ({u['calls']} reviews, ${u['cost_usd']:.4f})"])

        reports, cost, calls = [], 0.0, 0
        base = {"market_data": _market_data(signal),
                "account": {k: account.get(k) for k in ("equity", "open_positions", "day_pnl", "drawdown_pct")}}
        final = None
        try:
            # departments 1-4
            for d in DEPARTMENTS[:4]:
                if not any(task for _, task in d["analysts"]):
                    reports.append(self._no_data_dept(d))
                    continue
                rep, res = self._ask_department(d, signal, signal_id, base, DEPT_SCHEMA)
                calls += 1
                cost += res["cost_usd"]
                self._log_call(self._engine(d["provider"]), signal, signal_id, "REPORT", None,
                               [f"{d['lead']}: {rep['lead_summary'][:200]}"], res)
                reports.append(rep)
            # department 5: independent review (Grok) sees the other reports
            d5 = DEPARTMENTS[4]
            rv_ctx = {**base, "other_department_reports": [
                {"lead": r["lead"], "summary": r["lead_summary"], "stance": r["lead_stance"],
                 "analysts": [{k: a[k] for k in ("role", "finding", "stance")} for a in r["analysts"]]} for r in reports],
                "also_return": "verdict APPROVE or REJECT for the proposed paper trade, and verdict_confidence 0-1"}
            rev, res = self._ask_department(d5, signal, signal_id, rv_ctx, REVIEW_SCHEMA)
            calls += 1
            cost += res["cost_usd"]
            verdict = str(res["data"].get("verdict", "")).upper()
            vconf = float(res["data"].get("verdict_confidence", 0) or 0)
            rev["verdict"], rev["verdict_confidence"] = verdict, round(vconf, 2)
            eng5 = self._engine("grok")
            ok5 = verdict == "APPROVE" and vconf >= eng5.min_conf
            self._log_call(eng5, signal, signal_id, "APPROVE" if ok5 else "REJECT", vconf,
                           [f"Independent Review: {rev['lead_summary'][:240]}"], res)
            reports.append(rev)
            if not ok5:
                final = {"decision": "REJECT", "confidence": vconf,
                         "summary": f"Independent Research Review rejected ({verdict}, confidence {vconf:.2f}); "
                                    f"Chief not convened.",
                         "reasons": [rev["lead_summary"][:300]]}
            else:
                # Chief Market Intelligence Officer (Claude)
                chief_ctx = {**base, "department_reports": [
                    {"lead": r["lead"], "summary": r["lead_summary"], "stance": r["lead_stance"],
                     "analysts": [{k: a[k] for k in ("role", "finding", "stance", "confidence")} for a in r["analysts"]]}
                    for r in reports],
                    "independent_review_verdict": {"verdict": verdict, "confidence": vconf},
                    "your_job": "As Chief Market Intelligence Officer, weigh every department. Departments marked "
                                "no_data are genuinely missing, not neutral. APPROVE only if the evidence is clearly "
                                "strong; otherwise REJECT."}
                res = self.claude.call_json(BASE_SYSTEM, json.dumps(chief_ctx), CHIEF_SCHEMA, 900)
                calls += 1
                cost += res["cost_usd"]
                c = res["data"]
                dec = str(c.get("decision", "")).upper()
                conf = float(c.get("confidence", 0) or 0)
                if dec not in ("APPROVE", "REJECT"):
                    raise ValueError("chief returned no valid decision")
                reasons = [str(x)[:300] for x in (c.get("reasons") or [])][:6]
                if dec == "APPROVE" and conf < self.claude.min_conf:
                    reasons.append(f"confidence {conf:.2f} below threshold {self.claude.min_conf} -> REJECT")
                    dec = "REJECT"
                final = {"decision": dec, "confidence": conf, "summary": str(c.get("summary", ""))[:600],
                         "reasons": reasons, "_res": res}
        except Exception as exc:
            msg = type(exc).__name__ + ": " + str(exc)[:300]
            self.store.log("WARN", f"Research committee failed for {signal['symbol']}: {msg}")
            self.store.execute("INSERT INTO committee_runs(ts,symbol,bias,signal_id,decision,summary,reports,cost_usd,"
                               "ai_calls,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (t, signal["symbol"], signal["bias"], signal_id, "REJECT", "committee error",
                                json.dumps(reports), cost, calls, msg))
            return self.claude._record(signal, signal_id, "REJECT", None, ["Research committee error -> conservative reject"],
                                       self.claude.model, 0, 0, 0.0, error=msg)

        res = final.pop("_res", None)
        row = self._log_call(self.claude, signal, signal_id, final["decision"], final["confidence"],
                             [f"Chief: {final['summary'][:240]}"] + final["reasons"], res)
        self.store.execute("INSERT INTO committee_runs(ts,symbol,bias,signal_id,decision,confidence,summary,reports,"
                           "cost_usd,ai_calls) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (t, signal["symbol"], signal["bias"], signal_id, final["decision"], final["confidence"],
                            final["summary"], json.dumps(reports), cost, calls))
        self.store.log("INFO", f"Research committee {final['decision']} {signal['symbol']} {signal['bias']} "
                               f"(conf {final['confidence']:.2f}, {calls} AI calls, ${cost:.4f})")
        return {**row, "reasons": [f"Chief: {final['summary']}"] + final["reasons"], "cost_usd": cost}

    # ---------- dashboard / API ----------
    def latest(self, limit: int = 10) -> list[dict]:
        rows = self.store.query("SELECT * FROM committee_runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["reports"] = json.loads(r["reports"] or "[]")
        return rows

    def structure(self) -> dict:
        return {"chief": "Chief Market Intelligence Officer (Claude)",
                "departments": [{"lead": d["lead"], "color": d["color"],
                                 "engine": ("Grok" if d["provider"] == "grok" else "Claude") if d["provider"] else "no data source",
                                 "analysts": [{"role": r, "has_data": bool(t),
                                               "note": t or NO_DATA.get(r, "no data source")} for r, t in d["analysts"]]}
                                for d in DEPARTMENTS]}

    def selftest(self) -> dict:
        return {"claude": self.claude.selftest(), "grok": self.grok.selftest()}

    def status(self) -> dict:
        st = self.claude.status()
        st["grok"] = self.grok.status()
        st["review_mode"] = "research committee (Chief + 5 departments)"
        return st
