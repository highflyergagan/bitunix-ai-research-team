"""AI Research Committee: structure, honesty about missing data, approval rules, failures."""
import json
import unittest

from app.committee import DEPARTMENTS, ResearchCommittee
from app.scanner import Scanner
from app.store import Store
from tests.helpers import Clock, FakeClient, settings

SIG = {"symbol": "BTCUSDT", "bias": "LONG BIAS", "entry": 100, "stop_loss": 99, "take_profit": 102.5,
       "reasons": ["1h uptrend"], "indicators": {"rsi_15m": 60}, "data_gaps": ["news"]}
ACCT = {"equity": 100, "open_positions": 0, "day_pnl": 0, "drawdown_pct": 0}


class R:
    def __init__(self, j, status=200):
        self.status_code, self._j = status, j

    def json(self):
        return self._j


class FakeAI:
    """Routes Claude (forced tool) and Grok (json mode) calls; records every prompt."""

    def __init__(self, review="APPROVE", review_conf=0.8, chief="APPROVE", chief_conf=0.75, fail_claude=False):
        self.review, self.review_conf, self.chief, self.chief_conf = review, review_conf, chief, chief_conf
        self.fail_claude = fail_claude
        self.calls = []

    def __call__(self, url, headers, json, timeout):
        import json as js
        if "x.ai" in url:
            prompt = js.loads(json["messages"][1]["content"])
            self.calls.append(("grok", prompt))
            out = {"analysts": [{"role": a["role"], "finding": "ok", "stance": "neutral", "confidence": 0.6}
                                for a in prompt["your_analysts"]],
                   "lead_summary": "review", "lead_stance": "mixed", "verdict": self.review,
                   "verdict_confidence": self.review_conf}
            return R({"model": "grok-4.3", "choices": [{"message": {"content": js.dumps(out)}}],
                      "usage": {"prompt_tokens": 1000, "completion_tokens": 200}})
        if self.fail_claude:
            return R({"error": {"type": "overloaded_error", "message": "busy"}}, 529)
        prompt = js.loads(json["messages"][0]["content"])
        self.calls.append(("claude", prompt))
        if "decision" in json["tools"][0]["input_schema"]["properties"]:
            out = {"decision": self.chief, "confidence": self.chief_conf, "summary": "chief summary", "reasons": ["r1"]}
        else:
            out = {"analysts": [{"role": a["role"], "finding": "trend up", "stance": "bullish", "confidence": 0.7}
                                for a in prompt["your_analysts"]], "lead_summary": "dept ok", "lead_stance": "supports_trade"}
            if "verdict" in json["tools"][0]["input_schema"]["properties"]:
                out.update(verdict=self.review, verdict_confidence=self.review_conf)
        return R({"model": "claude-haiku-5-5", "usage": {"input_tokens": 800, "output_tokens": 150},
                  "content": [{"type": "tool_use", "name": "submit", "input": out}]})


class CommitteeTests(unittest.TestCase):
    def mk(self, ai=None, **kw):
        cfg = dict(ai_mode="required", anthropic_api_key="sk-ant-SECRET", xai_api_key="xai-SECRET", grok_mode="required")
        cfg.update(kw)
        self.s = settings(**cfg)
        self.store = Store(self.s.db_path)
        self.ai = ai or FakeAI()
        return ResearchCommittee(self.store, self.s, Clock(), self.ai)

    def test_org_chart_matches_structure(self):
        self.assertEqual([d["lead"] for d in DEPARTMENTS],
                         ["Market News Research Lead", "Equity Research Lead", "Head of Industry & Geopolitical Research",
                          "Head of Market Activity & Ownership Research", "Independent Research Review Lead"])
        self.assertEqual(sum(len(d["analysts"]) for d in DEPARTMENTS), 17)  # 17 analysts + 5 leads + chief = 23 roles

    def test_full_approval_flow(self):
        c = self.mk()
        r = c.review(SIG, ACCT)
        self.assertEqual(r["decision"], "APPROVE")
        providers = [p for p, _ in self.ai.calls]
        self.assertEqual(providers, ["claude", "claude", "grok", "claude"])  # news, activity, review, chief
        run = c.latest(1)[0]
        self.assertEqual(run["ai_calls"], 4)
        self.assertEqual(len(run["reports"]), 5)

    def test_missing_data_roles_are_never_sent_to_ai(self):
        c = self.mk()
        c.review(SIG, ACCT)
        asked = {a["role"] for _, p in self.ai.calls if "your_analysts" in p for a in p["your_analysts"]}
        for role in ("Insider Activity Analyst", "Fund Holdings Research Analyst", "News Credibility Analyst",
                     "Geopolitical Risk Analyst", "Financial Forensics Analyst"):
            self.assertNotIn(role, asked)
        reps = {r["lead"]: r for r in c.latest(1)[0]["reports"]}
        eq = reps["Equity Research Lead"]
        self.assertEqual(eq["lead_stance"], "no_data")
        self.assertTrue(all(a["stance"] == "no_data" for a in eq["analysts"]))
        ins = next(a for a in reps["Head of Market Activity & Ownership Research"]["analysts"]
                   if a["role"] == "Insider Activity Analyst")
        self.assertEqual(ins["stance"], "no_data")

    def test_review_reject_skips_chief(self):
        c = self.mk(FakeAI(review="REJECT"))
        r = c.review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")
        self.assertEqual([p for p, _ in self.ai.calls], ["claude", "claude", "grok"])

    def test_low_confidence_chief_rejects(self):
        self.assertEqual(self.mk(FakeAI(chief_conf=0.5)).review(SIG, ACCT)["decision"], "REJECT")

    def test_low_confidence_review_rejects(self):
        self.assertEqual(self.mk(FakeAI(review_conf=0.4)).review(SIG, ACCT)["decision"], "REJECT")

    def test_ai_failure_rejects_and_hides_keys(self):
        c = self.mk(FakeAI(fail_claude=True))
        r = c.review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")
        self.assertIn("529", r["error"])
        dump = json.dumps(self.store.query("SELECT * FROM ai_decisions") + self.store.query("SELECT * FROM committee_runs"))
        self.assertNotIn("SECRET", dump)

    def test_rejected_setup_not_reconvened(self):
        c = self.mk(FakeAI(review="REJECT"))
        c.review(SIG, ACCT)
        n = len(self.ai.calls)
        r = c.review(SIG, ACCT)
        self.assertEqual(r["decision"], "REJECT")
        self.assertEqual(len(self.ai.calls), n)
        self.assertIn("already rejected", r["reasons"][0])

    def test_grok_off_claude_runs_review(self):
        c = self.mk(xai_api_key="", grok_mode="auto")
        self.assertEqual(c.review(SIG, ACCT)["decision"], "APPROVE")
        self.assertEqual([p for p, _ in self.ai.calls], ["claude"] * 4)

    def test_department_reports_dont_burn_call_cap(self):
        c = self.mk()
        c.review(SIG, ACCT)
        self.assertEqual(c.claude.usage_today()["calls"], 1)  # only the chief's final decision counts

    def test_scanner_with_committee_opens_trade(self):
        s = settings(ai_mode="required", anthropic_api_key="k", xai_api_key="x", grok_mode="required")
        clock = Clock()
        sc = Scanner(Store(s.db_path), s, client=FakeClient(clock), clock=clock)
        sc.reviewer = ResearchCommittee(sc.store, s, clock, FakeAI())
        r = sc.scan_once()
        self.assertEqual(r["status"], "ok", r["errors"])
        self.assertGreaterEqual(r["opened"], 1)


if __name__ == "__main__":
    unittest.main()
