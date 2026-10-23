# Bitunix AI Research Desk — $100 PAPER-TRADING automation

> **PAPER TRADING ONLY.** No Bitunix credentials, no order/withdrawal code, no live trading.
> The app refuses to start if `LIVE_TRADING*` variables or Bitunix API keys are present.
> Signals are research hypotheses, not predictions. Nothing here guarantees profit.

## What it does

Every 5 minutes (aligned to :00, :05, :10 …) the scanner:

1. **Manages open paper positions** using *real subsequent* 1-minute Bitunix candles: a stop or target counts only if a later candle actually traded through it. If the stop and the target fall inside the same candle, it assumes the stop filled first. A gap through a level fills at the candle open. There's a 24h time stop, and funding is charged at each 8-hour settlement.
2. **Builds a universe** from your favorites (BTC, ETH, QNT, BR) plus the 12 most liquid USDT perpetuals by 24h volume.
3. **Computes a deterministic signal** for each symbol from closed 15m and 1h candles. It uses EMA 20/50 trend on both timeframes, MACD histogram, RSI (with an overbought/oversold block), ATR volatility band, swing support/resistance, volume expansion, a 24h liquidity floor (2M USDT) and a funding-rate crowding filter. Output: **LONG BIAS / SHORT BIAS / NO TRADE**, always with a stop (≥ max(1.5×ATR, 0.6%)) and a 2.5R target.
4. **Pre-checks risk.** Only candidates that pass go to **Claude** (`claude-haiku-5-5` by default) for an APPROVE/REJECT review. Claude cannot change size, stop or target. Errors, timeouts, malformed replies, confidence < 0.6, missing API key or an exhausted daily budget all mean **REJECT**.
5. **Re-checks risk, then opens a simulated position** with fees and slippage.

News, token unlocks, exchange listings and open interest have **no verified data source**. They are listed as unavailable in every signal and in Claude's prompt, and are never invented. Set `REQUIRE_VERIFIED_EVENT_FEEDS=true` to restore the original V4 rule that blocks every trade because of this.

### Risk controls (deterministic, cannot be overridden by AI)

| Control | Setting |
|---|---|
| Starting balance | 100 USDT (virtual) |
| Risk per trade | 0.5% of current equity, **including** fees + slippage at the stop |
| Daily loss | today's loss + open risk + new risk ≤ 2% of day-start equity; trading halts for the UTC day at −2% |
| Drawdown | drawdown + open risk + new risk ≤ 5% of peak; at −5% all paper positions close and trading halts until manual reset |
| Positions | max 2 open, one per symbol (also enforced by a unique DB index), 60-min cooldown per symbol, max 10 trades/day |
| Leverage cap | total notional ≤ 3× equity (the cap can only shrink a position) |
| Reward:risk | ≥ 1.5 **after** fees and slippage, otherwise rejected |
| Costs | 0.06% taker fee per side, 0.05% slippage (BTC/ETH) / 0.10% (other pairs) per fill |
| No martingale / DCA | there is no add-to-position code path |

Risk limits in `.env` can only be **tightened**. Values looser than the table above are ignored.

## Project layout

```
app/
  config.py      settings + hard paper-only guard
  bitunix.py     read-only public market-data client (GET allow-list only)
  indicators.py  EMA, RSI, MACD, ATR, swing levels
  strategy.py    signal engine
  risk.py        sizing + pre-trade checks
  paper.py       virtual account, exits from real candles, kill switches
  ai_review.py   Claude reviewer with call/spend caps
  scanner.py     5-minute loop, overlap lock
  report.py      24h experiment report  (python -m app.report)
  store.py       SQLite persistence (data/paper.db)
  main.py        FastAPI API + dashboard
  static/dashboard.html
  core.py, graph.py   original V4 research endpoints (kept; pip install -r requirements-research.txt for LangGraph)
tests/           safety, risk, paper exits, Claude safeguards, scanner, API auth
```

## Run it on Windows (simplest)

Install Python 3.12+ from python.org (tick "Add python.exe to PATH"), copy `.env.example` to `.env` and fill in `DASHBOARD_TOKEN` and `ANTHROPIC_API_KEY`, then double-click **start.bat**. Open http://127.0.0.1:8765. Keep the window open and the PC awake (Settings → System → Power → Sleep: Never) for the 24h test.

## Run it (Codespaces or any Linux box)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q                                   # all tests should pass

cp .env.example .env
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'   # paste into DASHBOARD_TOKEN
# add ANTHROPIC_API_KEY (set a low monthly spend limit on that key in the Anthropic console)

# check that Bitunix public data is reachable from this machine:
curl -s "https://fapi.bitunix.com/api/v1/futures/market/kline?symbol=BTCUSDT&interval=15m&limit=2"

set -a; . ./.env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
```

Always use **one worker**. The scanner runs inside the web process.

### Opening the dashboard

* **Codespaces:** open the **Ports** tab, find port 8765, keep visibility **Private**, and click the globe icon. Paste your `DASHBOARD_TOKEN` on the sign-in screen.
* **VPS:** keep the port on loopback and use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 user@server`), then open `http://127.0.0.1:8765`. Tailscale also works. Never expose the port publicly.

The token is kept only in that browser tab's sessionStorage. Every `/api/*` endpoint except `/health` requires it.

## The 24-hour paper test

1. Start the server, sign in, click **Scan now** once and check that signals appear with no errors.
2. Click **Start 24h test**. This records the start time and equity and starts automation. Alternatively: `curl -X POST -H "Authorization: Bearer $DASHBOARD_TOKEN" "http://127.0.0.1:8765/api/experiment/start?hours=24"`.
3. After 24h, click **Report** (or `GET /api/report`, or `python -m app.report` on the server).

The report only uses recorded data. It covers: scans completed vs expected, signals by bias, trades opened/closed/still open, wins/losses, net P&L after fees, slippage and funding, final equity, max drawdown, Claude calls, tokens and estimated cost, errors, and limitations. If no setup qualifies, it reports zero trades.

### Can GitHub Codespaces run it for 24 hours? No.

A codespace **stops after its idle timeout**: 30 minutes by default, and at most 240 minutes (GitHub → Settings → Codespaces → Default idle timeout). Background processes like uvicorn do **not** count as activity. Keep-alive tricks are unreliable and burn your free core-hours. Use Codespaces for development and short tests, and run the 24h test on an always-on host.

### Inexpensive persistent deployment (you choose and pay; nothing is purchased automatically)

Any small Ubuntu VPS with 1 vCPU and 1 GB RAM works (Hetzner, DigitalOcean, Vultr, Lightsail, or Oracle Cloud's Always Free tier). These typically cost a few dollars a month or nothing. **Before you rely on a server, run the `curl` check above from it.** Bitunix may block some regions or cloud IP ranges.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
git clone https://github.com/highflyergagan/bitunix-ai-research-team.git && cd bitunix-ai-research-team
cp .env.example .env && nano .env          # DASHBOARD_TOKEN, ANTHROPIC_API_KEY, AUTOSTART=true
sudo docker compose up --build -d
sudo docker compose exec desk pytest -q    # tests inside the container
curl http://127.0.0.1:8765/health
```

State lives in the `paper-data` Docker volume. With `AUTOSTART=true` (or if automation was running), scanning resumes automatically after a reboot or container restart. Exits are then caught up from the 1-minute candles that were missed.

## AI Research Committee (default review mode)

When the rule engine finds a setup that passes the risk pre-checks, an AI committee meets:

```
Chief Market Intelligence Officer (Claude)  -- final APPROVE/REJECT
├── Market News Research Lead (Claude): Research, Sentiment*, Stock Movement, News Credibility†, Market Impact†
├── Equity Research Lead: Fundamental†, Financial Forensics†, Pre-Earnings Intelligence†
├── Head of Industry & Geopolitical Research: Industry†, Geopolitical Risk†
├── Head of Market Activity & Ownership (Claude): Market Volume, Insider Activity†, Fund Holdings†, Short-Seller*
└── Independent Research Review Lead (Grok): Bull Case, Bear Case, Research Validation -> verdict
```
`*` uses the funding rate · `†` no data source is connected, so the analyst reports **no_data** with the reason and is
never asked to guess (no AI call is spent on it). A trade needs the Independent Review verdict AND the Chief to approve
with >= 0.6 confidence. About 4 AI calls per meeting (~$0.002-0.005). A rejected setup is not re-sent for 60 minutes.
Set `REVIEW_MODE=panel` for the simpler Claude-then-Grok check.

## Study mode (research only)

Every scan also records coins that moved +/-30% in 24h (min $200k volume) and follows them for 24h, including a
hypothetical "short after pump" result (before fees). No trades are ever placed from study data. See `/api/study`.

## Claude API cost

Each review is about 1–1.5k input tokens and under 150 output tokens. That's roughly $0.0002 per call at Haiku 5.5 list prices ($0.10 / $0.50 per million tokens). The defaults cap usage at 40 calls and $0.50 per day. Claude is called only for candidates that already passed the rules, so on most scans it is not called at all. Update the `CLAUDE_*_USD_PER_MTOK` variables if pricing changes.

## Known limitations

* Simulated fills: no order book, partial fills, liquidation or queue position. Funding uses the current rate as an estimate.
* Entries start on the minute after the signal; moves inside the entry minute are not counted.
* The strategy rules are reasonable heuristics, not a validated edge. A 24-hour sample is far too small to judge them.
* The Bitunix funding-rate endpoint is optional. If it isn't available, the signal records `funding_rate` as a data gap and continues.
