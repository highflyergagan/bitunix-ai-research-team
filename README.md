# Bitunix Research Desk V4 — PAPER ONLY

A custom LangGraph implementation **inspired by** Tauric Research TradingAgents, not a fork or bundled copy of their original package. No API keys, authenticated Bitunix requests, live orders, or real-money trading code. The 23 roles are research labels/deterministic checks, **not 23 independent LLMs**. Five actual LangGraph department nodes plus a Chief node run sequentially. News, exchange listings, token unlocks, whales, funding and open interest have no verified providers yet; their unavailable status causes a NO TRADE veto. Bull/base/bear ranges are heuristic, not calibrated forecasts. Market scanner uses all tickers returned by Bitunix public futures API (subject to API availability), ranks by 24h quote volume, and performs detailed research on a selected symbol. The dashboard has no automated scheduler or trade journal yet.

## Prerequisites
Ubuntu Linux VPS (x86_64 preferred), Docker Engine and Docker Compose plugin, SSH access, Python 3.11+ for optional local tests. A phone alone does not provide the VPS. Do not expose port 8765 publicly.

## Deploy on Ubuntu server
```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 python3 python3-venv unzip
sudo systemctl enable --now docker
unzip bitunix_team_v4.zip
cd bitunix_team_v4
cp .env.example .env
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
# Edit .env and replace REPLACE_WITH_RANDOM_TOKEN with the generated secret
nano .env
sudo docker compose up --build -d
sudo docker compose logs --tail=30
curl http://127.0.0.1:8765/health
```
If `docker-compose-v2` isn't available, install Docker's official Compose plugin for your Ubuntu release. No need to install Python libraries in Android Termux.

## Android access — SSH tunnel
Install an Android SSH client that supports local port forwarding; forward local `127.0.0.1:8765` to server `127.0.0.1:8765` over SSH. In Samsung Chrome open `http://127.0.0.1:8765`, enter the DASHBOARD_TOKEN. Alternative: use Tailscale with appropriate access controls, but never open unauthenticated ports to the internet. HTTP loopback is safe only through a trusted tunnel.

## Tests on Linux
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Data / risk limitations
The `/api/scan` endpoint fetches current public Bitunix tickers and `/api/research/BTCUSDT` fetches 1h candles. Data retrieval requires internet and may fail or be rate-limited. All unverified event feeds cause NO TRADE. The `/api/paper-example` endpoint calculates hypothetical position sizing only; it does not create orders or record a ledger. The simulated max loss ignores fees, slippage, gaps, liquidation, and funding; do not interpret it as a guaranteed loss cap. There is no profitability guarantee. For Tauric's official framework, see https://github.com/TauricResearch/TradingAgents . This project uses LangGraph directly, not the official TradingAgents implementation.
