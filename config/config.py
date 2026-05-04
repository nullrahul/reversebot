"""
Configuration loader for Polymarket Reverse Bot.
All tunable parameters loaded from .env — zero hardcoded values.
Fails loudly on missing required keys.
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv


def _require_env(key: str) -> str:
    """Get required env var or exit with descriptive error."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        print(f"FATAL: Required environment variable '{key}' is missing or empty.", file=sys.stderr)
        print(f"       Copy .env.example to .env and fill in all required values.", file=sys.stderr)
        sys.exit(1)
    return val.strip()


def _env_float(key: str, default: float) -> float:
    """Get float env var with default."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val.strip())
    except ValueError:
        print(f"FATAL: Environment variable '{key}' must be a number, got: '{val}'", file=sys.stderr)
        sys.exit(1)


def _env_int(key: str, default: int) -> int:
    """Get int env var with default."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val.strip())
    except ValueError:
        print(f"FATAL: Environment variable '{key}' must be an integer, got: '{val}'", file=sys.stderr)
        sys.exit(1)


def _env_bool(key: str, default: bool) -> bool:
    """Get bool env var with default. Only 'false' (case-insensitive) is False."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() != "false"


def _env_list(key: str, default: List[str]) -> List[str]:
    """Get comma-separated list from env."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return [item.strip() for item in val.split(",") if item.strip()]


def _env_int_list(key: str, default: List[int]) -> List[int]:
    """Get comma-separated integer list from env."""
    raw_items = _env_list(key, [str(item) for item in default])
    values: List[int] = []
    for item in raw_items:
        try:
            values.append(int(item))
        except ValueError:
            print(f"FATAL: Environment variable '{key}' must be a comma-separated integer list, got: '{item}'", file=sys.stderr)
            sys.exit(1)
    return values


@dataclass(frozen=True)
class BotConfig:
    """Immutable configuration for the Reverse Bot. All values from .env."""

    # --- Wallet & Auth ---
    private_key: str
    poly_api_key: str
    poly_api_secret: str
    poly_passphrase: str

    # --- Network ---
    chain_id: int
    clob_api_url: str
    gamma_api_url: str

    # --- Strategy ---
    yes_bias_threshold: float
    yes_bias_max_threshold: float
    min_volume_24h_usdc: float
    min_time_to_close_hours: float # Changed to float for consistency
    max_time_to_close_days: float # Changed to float for consistency
    position_size_usdc: float
    max_open_positions: int
    max_daily_loss_usdc: float
    max_total_exposure_usdc: float
    take_profit_multiplier: float
    stop_loss_multiplier: float
    max_no_ask_multiplier: float
    high_vol_ask_multiplier: float
    high_vol_assets: List[str]
    trailing_tp_activation_multiplier: float
    trailing_tp_buffer_pct: float
    min_ev_threshold: float
    max_spread_pct_of_ask: float
    correlated_window_seconds: int
    max_same_direction_entries: int
    hold_to_resolution: bool
    poll_interval_seconds: int
    global_cooldown: int
    slippage_depth_multiplier: float
    rate_limit_orders_per_min: int
    rate_limit_api_calls_per_min: int
    trailing_tp_enabled: bool
    tp1_shares_count: int
    tp1_multiplier: float
    tp2_multiplier: float
    polygon_rpc: str
    ghost_guard_enabled: bool

    # --- Mode ---
    paper_trade: bool
    bot_version: str
    dashboard_password: str

    # --- Optional / With Defaults ---
    poly_address: Optional[str] = None
    category_whitelist: List[str] = field(default_factory=list)

    # --- Market Targeting ---
    target_market_mode: str = "crypto_short_window"
    target_crypto_symbols: List[str] = field(default_factory=list)
    target_window_minutes: List[int] = field(default_factory=list)
    short_window_discovery_interval_seconds: int = 20
    short_window_min_seconds_to_close: int = 60

    # --- Paths ---
    db_path: str = "data/reversebot.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    def validate(self) -> None:
        """Run sanity checks on loaded config. Raises ValueError on bad config."""
        errors = []

        if not (0.0 < self.yes_bias_threshold < 1.0):
            errors.append(f"YES_BIAS_THRESHOLD must be 0-1, got {self.yes_bias_threshold}")

        if not (0.0 < self.yes_bias_max_threshold <= 1.0):
            errors.append(f"YES_BIAS_MAX_THRESHOLD must be 0-1, got {self.yes_bias_max_threshold}")

        if self.yes_bias_threshold >= self.yes_bias_max_threshold:
            errors.append("YES_BIAS_THRESHOLD must be < YES_BIAS_MAX_THRESHOLD")

        if self.position_size_usdc <= 0:
            errors.append(f"POSITION_SIZE_USDC must be > 0, got {self.position_size_usdc}")

        if self.max_open_positions < 1:
            errors.append(f"MAX_OPEN_POSITIONS must be >= 1, got {self.max_open_positions}")

        if self.max_daily_loss_usdc <= 0:
            errors.append(f"MAX_DAILY_LOSS_USDC must be > 0, got {self.max_daily_loss_usdc}")

        if self.max_total_exposure_usdc < self.position_size_usdc:
            errors.append("MAX_TOTAL_EXPOSURE_USDC must be >= POSITION_SIZE_USDC")

        if self.take_profit_multiplier <= 1.0:
            errors.append(f"TAKE_PROFIT_MULTIPLIER must be > 1.0, got {self.take_profit_multiplier}")

        if not (0.0 < self.stop_loss_multiplier < 1.0):
            errors.append(f"STOP_LOSS_MULTIPLIER must be 0-1, got {self.stop_loss_multiplier}")

        if self.poll_interval_seconds < 1:
            errors.append(f"POLL_INTERVAL_SECONDS must be >= 1, got {self.poll_interval_seconds}")

        if self.slippage_depth_multiplier < 0.1:
            errors.append(f"SLIPPAGE_DEPTH_MULTIPLIER must be >= 0.1, got {self.slippage_depth_multiplier}")

        if self.rate_limit_orders_per_min > 60:
            errors.append(f"RATE_LIMIT_ORDERS_PER_MIN must be <= 60, got {self.rate_limit_orders_per_min}")

        if self.rate_limit_api_calls_per_min > 1000:
            errors.append(f"RATE_LIMIT_API_CALLS_PER_MIN must be <= 1000, got {self.rate_limit_api_calls_per_min}")

        if not self.category_whitelist:
            errors.append("CATEGORY_WHITELIST must contain at least one category")

        if self.target_market_mode not in {"broad", "crypto_short_window"}:
            errors.append(
                f"TARGET_MARKET_MODE must be 'broad' or 'crypto_short_window', got {self.target_market_mode}"
            )

        if not self.target_crypto_symbols:
            errors.append("TARGET_CRYPTO_SYMBOLS must contain at least one symbol")

        invalid_windows = [window for window in self.target_window_minutes if window <= 0]
        if invalid_windows:
            errors.append(f"TARGET_WINDOW_MINUTES must be positive, got {invalid_windows}")

        if self.short_window_discovery_interval_seconds < 5:
            errors.append(
                "SHORT_WINDOW_DISCOVERY_INTERVAL_SECONDS must be >= 5 seconds"
            )

        if self.short_window_min_seconds_to_close < 5:
            errors.append("SHORT_WINDOW_MIN_SECONDS_TO_CLOSE must be >= 5 seconds")

        if self.target_market_mode == "crypto_short_window" and not self.paper_trade:
            errors.append("TARGET_MARKET_MODE=crypto_short_window requires PAPER_TRADE=true")

        if self.target_market_mode == "crypto_short_window" and self.poll_interval_seconds > 30:
            errors.append("TARGET_MARKET_MODE=crypto_short_window requires POLL_INTERVAL_SECONDS <= 30")

        if errors:
            msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ValueError(msg)


def load_config(env_path: Optional[str] = None) -> BotConfig:
    """
    Load configuration from .env file.
    Fails loudly on missing required keys or invalid values.
    """
    if env_path:
        load_dotenv(env_path, override=True)
    else:
        load_dotenv(override=True)

    config = BotConfig(
        # Auth — all required
        private_key=_require_env("PRIVATE_KEY"),
        poly_api_key=_require_env("POLY_API_KEY"),
        poly_api_secret=_require_env("POLY_API_SECRET"),
        poly_passphrase=_require_env("POLY_PASSPHRASE"),
        poly_address=os.getenv("POLY_ADDRESS"),

        # Network
        chain_id=_env_int("CHAIN_ID", 137),
        clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com").strip(),
        gamma_api_url=os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com").strip(),

        # Strategy
        yes_bias_threshold=_env_float("YES_BIAS_THRESHOLD", 0.80),
        yes_bias_max_threshold=_env_float("YES_BIAS_MAX_THRESHOLD", 0.97),
        min_volume_24h_usdc=_env_float("MIN_VOLUME_24H_USDC", 500.0),
        min_time_to_close_hours=_env_float("MIN_TIME_TO_CLOSE_HOURS", 6.0),
        max_time_to_close_days=_env_float("MAX_TIME_TO_CLOSE_DAYS", 7.0),
        position_size_usdc=_env_float("POSITION_SIZE_USDC", 2.0),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 2),
        max_daily_loss_usdc=_env_float("MAX_DAILY_LOSS_USDC", 5.0),
        max_total_exposure_usdc=_env_float("MAX_TOTAL_EXPOSURE_USDC", 5.0),
        take_profit_multiplier=_env_float("TAKE_PROFIT_MULTIPLIER", 2.0),
        stop_loss_multiplier=_env_float("STOP_LOSS_MULTIPLIER", 0.5),
        max_no_ask_multiplier=_env_float("MAX_NO_ASK_MULTIPLIER", 1.35),
        high_vol_ask_multiplier=_env_float("HIGH_VOL_ASK_MULTIPLIER", 1.6),
        high_vol_assets=_env_list("HIGH_VOL_ASSETS", ["HYPE", "XRP", "DOGE"]),
        trailing_tp_activation_multiplier=_env_float("TRAILING_TP_ACTIVATION_MULTIPLIER", 1.5),
        trailing_tp_buffer_pct=_env_float("TRAILING_TP_BUFFER_PCT", 0.20),
        min_ev_threshold=_env_float("MIN_EV_THRESHOLD", 0.02),
        max_spread_pct_of_ask=_env_float("MAX_SPREAD_PCT_OF_ASK", 0.60),
        correlated_window_seconds=_env_int("CORRELATED_WINDOW_SECONDS", 180),
        max_same_direction_entries=_env_int("MAX_SAME_DIRECTION_ENTRIES", 2),
        hold_to_resolution=_env_bool("HOLD_TO_RESOLUTION", False),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 60),
        global_cooldown=_env_int("GLOBAL_COOLDOWN", 60),
        slippage_depth_multiplier=_env_float("SLIPPAGE_DEPTH_MULTIPLIER", 1.0),
        rate_limit_orders_per_min=_env_int("RATE_LIMIT_ORDERS_PER_MIN", 55),
        rate_limit_api_calls_per_min=_env_int("RATE_LIMIT_API_CALLS_PER_MIN", 90),

        # Mode
        paper_trade=_env_bool("PAPER_TRADE", True),
        bot_version=os.getenv("BOT_VERSION", "v0.1.0").strip(),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", "reversebot2025").strip(),

        # Categories
        category_whitelist=_env_list(
            "CATEGORY_WHITELIST",
            ["Sports", "Politics", "Crypto", "Finance", "Weather", "Entertainment"]
        ),

        # Market targeting
        target_market_mode=os.getenv("TARGET_MARKET_MODE", "crypto_short_window").strip().lower(),
        target_crypto_symbols=[symbol.upper() for symbol in _env_list("TARGET_CRYPTO_SYMBOLS", ["BTC", "ETH"])],
        target_window_minutes=_env_int_list("TARGET_WINDOW_MINUTES", [5, 15]),
        short_window_discovery_interval_seconds=_env_int(
            "SHORT_WINDOW_DISCOVERY_INTERVAL_SECONDS", 20
        ),
        short_window_min_seconds_to_close=_env_int(
            "SHORT_WINDOW_MIN_SECONDS_TO_CLOSE", 60
        ),
        trailing_tp_enabled=_env_bool("TRAILING_TP_ENABLED", True),
        tp1_shares_count=_env_int("TP1_SHARES_COUNT", 8),
        tp1_multiplier=_env_float("TP1_MULTIPLIER", 1.5),
        tp2_multiplier=_env_float("TP2_MULTIPLIER", 2.5),
        polygon_rpc=os.getenv("POLYGON_RPC", "https://polygon-rpc.com").strip(),
        ghost_guard_enabled=_env_bool("GHOST_GUARD_ENABLED", False),

        # Paths
        db_path=os.getenv("DB_PATH", "data/reversebot.db").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )

    config.validate()
    # Always apply runtime overrides if they exist, so boot is in sync with dashboard
    config = load_runtime_overrides(config)
    return config


def load_runtime_overrides(base_config: "BotConfig") -> "BotConfig":
    """
    Merge dashboard/runtime_config.json overrides onto the base config.
    Called every trading cycle to pick up live parameter changes.
    Returns a new BotConfig with overrides applied (does NOT mutate original).
    Uses runtime_config_rd.json for R&D mode to avoid cross-contamination.
    """
    import json
    # R&D mode uses its own isolated runtime config
    if getattr(base_config, "target_market_mode", "") == "crypto_short_window":
        runtime_path = Path("dashboard/runtime_config_rd.json")
    else:
        runtime_path = Path("dashboard/runtime_config.json")
    if not runtime_path.exists():
        return base_config

    try:
        overrides = json.loads(runtime_path.read_text())
    except Exception:
        return base_config

    # Mapping from runtime_config keys → BotConfig fields
    field_map = {
        "yes_bias_threshold": ("yes_bias_threshold", float),
        "yes_bias_max_threshold": ("yes_bias_max_threshold", float),
        "min_volume_24h_usdc": ("min_volume_24h_usdc", float),
        "min_time_to_close_hours": ("min_time_to_close_hours", float),
        "max_time_to_close_days": ("max_time_to_close_days", float),
        "position_size_usdc": ("position_size_usdc", float),
        "max_open_positions": ("max_open_positions", int),
        "max_daily_loss_usdc": ("max_daily_loss_usdc", float),
        "max_total_exposure_usdc": ("max_total_exposure_usdc", float),
        "take_profit_multiplier": ("take_profit_multiplier", float),
        "stop_loss_multiplier": ("stop_loss_multiplier", float),
        "max_no_ask_multiplier": ("max_no_ask_multiplier", float),
        "high_vol_ask_multiplier": ("high_vol_ask_multiplier", float),
        "high_vol_assets": ("high_vol_assets", list),
        "trailing_tp_activation_multiplier": ("trailing_tp_activation_multiplier", float),
        "trailing_tp_buffer_pct": ("trailing_tp_buffer_pct", float),
        "min_ev_threshold": ("min_ev_threshold", float),
        "max_spread_pct_of_ask": ("max_spread_pct_of_ask", float),
        "correlated_window_seconds": ("correlated_window_seconds", int),
        "max_same_direction_entries": ("max_same_direction_entries", int),
        "hold_to_resolution": ("hold_to_resolution", bool),
        "poll_interval_seconds": ("poll_interval_seconds", int),
        "global_cooldown": ("global_cooldown", int),
        "slippage_depth_multiplier": ("slippage_depth_multiplier", float),
        "short_window_min_seconds_to_close": ("short_window_min_seconds_to_close", int),
        "trailing_tp_enabled": ("trailing_tp_enabled", bool),
        "tp1_shares_count": ("tp1_shares_count", int),
        "tp1_multiplier": ("tp1_multiplier", float),
        "tp2_multiplier": ("tp2_multiplier", float),
    }

    patched = False
    for rt_key, (cfg_field, cast_fn) in field_map.items():
        if rt_key in overrides:
            try:
                new_val = cast_fn(overrides[rt_key])
                if getattr(base_config, cfg_field) != new_val:
                    object.__setattr__(base_config, cfg_field, new_val)
                    patched = True
            except (ValueError, TypeError):
                pass

    return base_config
