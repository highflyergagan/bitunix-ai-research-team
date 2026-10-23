"""Central configuration. Everything is read from environment variables.

PAPER TRADING ONLY. There is deliberately no setting that enables live trading:
`LIVE_TRADING_ENABLED` is a hard-coded constant, and the app refuses to start if
anyone tries to switch it on through the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Hard-coded safety constants -- not configurable.
LIVE_TRADING_ENABLED = False
PAPER_ONLY = True


class LiveTradingRequestedError(RuntimeError):
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [s.strip().upper() for s in os.getenv(name, default).split(",") if s.strip()]


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Never overrides variables already set."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def assert_paper_only() -> None:
    """Refuse to run if anything in the environment asks for live trading."""
    for key in ("LIVE_TRADING", "LIVE_TRADING_ENABLED", "ENABLE_LIVE_TRADING", "REAL_TRADING"):
        if _b(key, False):
            raise LiveTradingRequestedError(
                f"{key} is set, but this system is PAPER TRADING ONLY. Live trading is not supported."
            )
    if os.getenv("BITUNIX_API_KEY") or os.getenv("BITUNIX_SECRET_KEY"):
        raise LiveTradingRequestedError(
            "Bitunix API keys were found in the environment. This app only uses public market data; "
            "remove BITUNIX_API_KEY / BITUNIX_SECRET_KEY."
        )
    if LIVE_TRADING_ENABLED or not PAPER_ONLY:
        raise LiveTradingRequestedError("Live trading constants were modified.")


@dataclass
class Settings:
    # Account & risk
    starting_balance: float = 100.0
    risk_per_trade: float = 0.005  # 0.5% of current equity
    max_daily_loss: float = 0.02  # 2% of start-of-day equity
    max_drawdown: float = 0.05  # 5% from peak equity
    max_open_positions: int = 2
    max_leverage: float = 3.0  # notional cap = equity * max_leverage
    min_notional: float = 5.0
    max_trades_per_day: int = 10
    symbol_cooldown_minutes: int = 60
    max_hold_hours: float = 24.0
    min_reward_risk: float = 1.5  # after fees/slippage
    # Costs
    taker_fee: float = 0.0006  # 0.06% per side
    slippage: float = 0.0005  # 0.05% per fill (majors)
    alt_extra_slippage: float = 0.0005  # added for non-BTC/ETH pairs
    # Scanner
    scan_interval_seconds: int = 300
    universe_top_n: int = 12
    favorites: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "QNTUSDT", "BRUSDT"])
    min_quote_volume: float = 5_000_000.0
    request_pause_seconds: float = 0.15
    autostart: bool = False
    require_verified_event_feeds: bool = False
    # Claude
    anthropic_api_key: str = field(default="", repr=False)
    ai_mode: str = "required"  # required | optional | off
    claude_model: str = "claude-haiku-5-5"
    claude_max_calls_per_day: int = 40
    claude_max_daily_usd: float = 0.50
    claude_input_usd_per_mtok: float = 0.10
    claude_output_usd_per_mtok: float = 0.50
    claude_min_confidence: float = 0.6
    claude_timeout_seconds: float = 30.0
    # App
    dashboard_token: str = field(default="", repr=False)
    data_dir: str = "data"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "paper.db")

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.starting_balance = _f("STARTING_BALANCE", s.starting_balance)
        # Risk limits may only be tightened via env, never loosened past the defaults.
        s.risk_per_trade = min(_f("RISK_PER_TRADE", s.risk_per_trade), 0.005)
        s.max_daily_loss = min(_f("MAX_DAILY_LOSS", s.max_daily_loss), 0.02)
        s.max_drawdown = min(_f("MAX_DRAWDOWN", s.max_drawdown), 0.05)
        s.max_open_positions = max(0, min(_i("MAX_OPEN_POSITIONS", s.max_open_positions), 2))
        s.max_leverage = max(1.0, min(_f("MAX_LEVERAGE", s.max_leverage), 3.0))
        s.max_trades_per_day = _i("MAX_TRADES_PER_DAY", s.max_trades_per_day)
        s.taker_fee = max(_f("TAKER_FEE", s.taker_fee), 0.0)
        s.slippage = max(_f("SLIPPAGE", s.slippage), 0.0)
        s.scan_interval_seconds = max(60, _i("SCAN_INTERVAL_SECONDS", s.scan_interval_seconds))
        s.universe_top_n = _i("UNIVERSE_TOP_N", s.universe_top_n)
        s.favorites = _list("FAVORITES", ",".join(s.favorites))
        s.min_quote_volume = _f("MIN_QUOTE_VOLUME", s.min_quote_volume)
        s.autostart = _b("AUTOSTART", s.autostart)
        s.require_verified_event_feeds = _b("REQUIRE_VERIFIED_EVENT_FEEDS", s.require_verified_event_feeds)
        s.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        mode = os.getenv("AI_MODE", s.ai_mode).strip().lower()
        s.ai_mode = mode if mode in ("required", "optional", "off") else "required"
        s.claude_model = os.getenv("CLAUDE_MODEL", s.claude_model)
        s.claude_max_calls_per_day = _i("CLAUDE_MAX_CALLS_PER_DAY", s.claude_max_calls_per_day)
        s.claude_max_daily_usd = _f("CLAUDE_MAX_DAILY_USD", s.claude_max_daily_usd)
        s.claude_input_usd_per_mtok = _f("CLAUDE_INPUT_USD_PER_MTOK", s.claude_input_usd_per_mtok)
        s.claude_output_usd_per_mtok = _f("CLAUDE_OUTPUT_USD_PER_MTOK", s.claude_output_usd_per_mtok)
        s.claude_min_confidence = _f("CLAUDE_MIN_CONFIDENCE", s.claude_min_confidence)
        s.dashboard_token = os.getenv("DASHBOARD_TOKEN", "")
        s.data_dir = os.getenv("DATA_DIR", s.data_dir)
        return s
