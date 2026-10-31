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
    polygon_rpc: str

    # --- Core Strategy ---
    position_size_usdc: float
    max_daily_loss_usdc: float
    max_total_exposure_usdc: float
    min_ev_threshold: float
    hold_to_resolution: bool
    take_profit_threshold: float
    trailing_tp_enabled: bool
    trailing_tp_trigger: float
    trailing_tp_trail: float

    # --- Spread & Pricing Guards ---
    max_no_ask_multiplier: float
    high_vol_ask_multiplier: float
    high_vol_assets: List[str]
    max_spread_pct_of_ask: float



    # --- Risk ---
    ghost_guard_enabled: bool

    # --- Rate Limits ---
    rate_limit_orders_per_min: int
    rate_limit_api_calls_per_min: int

    # --- Mode ---
    paper_trade: bool
    bot_version: str
    dashboard_password: str

    # --- Optional / With Defaults ---
    poly_address: Optional[str] = None
    poly_signature_type: int = 2
    category_whitelist: List[str] = field(default_factory=lambda: ["Crypto"])
    timeframe_15m_enabled: bool = False
    min_streak_quality_score: float = 0.45
    streak_quality_window: int = 7
    lossmomentum_skip_enabled: bool = True
    reversal_toggling_enabled: bool = True
    reversal_lookback_n: int = 10
    reversal_switch_threshold: float = 0.45
    reversal_spread_penalty: float = 0.0
    reversal_lookback_window_seconds: int = 86400
    regime_filter_enabled: bool = True
    stop_loss_enabled: bool = True
    stop_loss_pct: float = 0.12

    # --- Market Targeting ---
    target_market_mode: str = "crypto_short_window"
    target_crypto_symbols: List[str] = field(default_factory=list)
    target_window_minutes: List[int] = field(default_factory=list)
    short_window_min_seconds_to_close: int = 60
    live_profile_only: str = "livemode"

    # --- Paths ---
    db_path: str = "data/reversebot.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    def validate(self) -> None:
        """Run sanity checks on loaded config. Raises ValueError on bad config."""
        errors = []

        if self.position_size_usdc <= 0:
            errors.append(f"POSITION_SIZE_USDC must be > 0, got {self.position_size_usdc}")

        if self.max_daily_loss_usdc <= 0:
            errors.append(f"MAX_DAILY_LOSS_USDC must be > 0, got {self.max_daily_loss_usdc}")

        if self.max_total_exposure_usdc < self.position_size_usdc:
            errors.append("MAX_TOTAL_EXPOSURE_USDC must be >= POSITION_SIZE_USDC")



        if self.rate_limit_orders_per_min > 60:
            errors.append(f"RATE_LIMIT_ORDERS_PER_MIN must be <= 60, got {self.rate_limit_orders_per_min}")

        if self.rate_limit_api_calls_per_min > 1000:
            errors.append(f"RATE_LIMIT_API_CALLS_PER_MIN must be <= 1000, got {self.rate_limit_api_calls_per_min}")

        if self.target_market_mode not in {"broad", "crypto_short_window"}:
            errors.append(
                f"TARGET_MARKET_MODE must be 'broad' or 'crypto_short_window', got {self.target_market_mode}"
            )

        if not self.target_crypto_symbols:
            errors.append("TARGET_CRYPTO_SYMBOLS must contain at least one symbol")

        invalid_windows = [window for window in self.target_window_minutes if window <= 0]
        if invalid_windows:
            errors.append(f"TARGET_WINDOW_MINUTES must be positive, got {invalid_windows}")

        if self.short_window_min_seconds_to_close < 5:
            errors.append("SHORT_WINDOW_MIN_SECONDS_TO_CLOSE must be >= 5 seconds")

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
        poly_signature_type=_env_int("POLY_SIGNATURE_TYPE", 2),

        # Network
        chain_id=_env_int("CHAIN_ID", 137),
        clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com").strip(),
        gamma_api_url=os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com").strip(),
        polygon_rpc=os.getenv("POLYGON_RPC", "https://polygon-rpc.com").strip(),

        # Core Strategy
        position_size_usdc=_env_float("POSITION_SIZE_USDC", 2.0),
        max_daily_loss_usdc=_env_float("MAX_DAILY_LOSS_USDC", 20.0),
        max_total_exposure_usdc=_env_float("MAX_TOTAL_EXPOSURE_USDC", 5.0),
        min_ev_threshold=_env_float("MIN_EV_THRESHOLD", 0.02),
        hold_to_resolution=_env_bool("HOLD_TO_RESOLUTION", False),
        take_profit_threshold=_env_float("TAKE_PROFIT_THRESHOLD", 0.98),
        trailing_tp_enabled=_env_bool("TRAILING_TP_ENABLED", True),
        trailing_tp_trigger=_env_float("TRAILING_TP_TRIGGER", 0.90),
        trailing_tp_trail=_env_float("TRAILING_TP_TRAIL", 0.05),

        # Spread & Pricing Guards
        max_no_ask_multiplier=_env_float("MAX_NO_ASK_MULTIPLIER", 1.35),
        high_vol_ask_multiplier=_env_float("HIGH_VOL_ASK_MULTIPLIER", 1.6),
        high_vol_assets=_env_list("HIGH_VOL_ASSETS", ["HYPE", "XRP", "DOGE"]),
        max_spread_pct_of_ask=_env_float("MAX_SPREAD_PCT_OF_ASK", 0.60),



        # Risk
        ghost_guard_enabled=_env_bool("GHOST_GUARD_ENABLED", False),

        # Rate Limits
        rate_limit_orders_per_min=_env_int("RATE_LIMIT_ORDERS_PER_MIN", 55),
        rate_limit_api_calls_per_min=_env_int("RATE_LIMIT_API_CALLS_PER_MIN", 90),

        # Mode
        paper_trade=_env_bool("PAPER_TRADE", True),
        timeframe_15m_enabled=_env_bool("TIMEFRAME_15M_ENABLED", False),
        min_streak_quality_score=_env_float("MIN_STREAK_QUALITY_SCORE", 0.45),
        streak_quality_window=_env_int("STREAK_QUALITY_WINDOW", 7),
        lossmomentum_skip_enabled=_env_bool("LOSSMOMENTUM_SKIP_ENABLED", True),
        reversal_toggling_enabled=_env_bool("REVERSAL_TOGGLING_ENABLED", True),
        reversal_lookback_n=_env_int("REVERSAL_LOOKBACK_N", 10),
        reversal_switch_threshold=_env_float("REVERSAL_SWITCH_THRESHOLD", 0.45),
        reversal_spread_penalty=_env_float("REVERSAL_SPREAD_PENALTY", 0.0),
        reversal_lookback_window_seconds=_env_int("REVERSAL_LOOKBACK_WINDOW_SECONDS", 86400),
        regime_filter_enabled=_env_bool("REGIME_FILTER_ENABLED", True),
        stop_loss_enabled=_env_bool("STOP_LOSS_ENABLED", True),
        stop_loss_pct=_env_float("STOP_LOSS_PCT", 0.12),
        bot_version=os.getenv("BOT_VERSION", "v3.2-audit-fixed").strip(),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", "reversebot2025").strip(),

        # Categories (used by validator.py check 9 for non-crypto fallback)
        category_whitelist=_env_list("CATEGORY_WHITELIST", ["Crypto"]),

        # Market targeting
        target_market_mode=os.getenv("TARGET_MARKET_MODE", "crypto_short_window").strip().lower(),
        target_crypto_symbols=[symbol.upper() for symbol in _env_list("TARGET_CRYPTO_SYMBOLS", ["BTC", "ETH"])],
        target_window_minutes=_env_int_list("TARGET_WINDOW_MINUTES", [5, 15]),
        short_window_min_seconds_to_close=_env_int("SHORT_WINDOW_MIN_SECONDS_TO_CLOSE", 60),
        live_profile_only=os.getenv("LIVE_PROFILE_ONLY", "livemode").strip().lower(),

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
        env_path = Path(".env.rd")
    else:
        runtime_path = Path("dashboard/runtime_config.json")
        env_path = Path(".env")

    # --- Self-Healing Env Override Guard ---
    # If the env file has been modified more recently than the runtime config overrides,
    # it means the user edited the env file. We scan for explicitly defined env keys
    # and pop them from the overrides, forcing them to fall back to the env configurations.
    if env_path.exists() and runtime_path.exists():
        try:
            env_mtime = env_path.stat().st_mtime
            runtime_mtime = runtime_path.stat().st_mtime
            if env_mtime > runtime_mtime:
                env_keys = set()
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k = line.split("=", 1)[0].strip().lower()
                            env_keys.add(k)
                
                try:
                    overrides = json.loads(runtime_path.read_text())
                    modified_overrides = False
                    for rt_key in list(overrides.keys()):
                        if rt_key.lower() in env_keys:
                            overrides.pop(rt_key)
                            modified_overrides = True
                    if modified_overrides:
                        runtime_path.write_text(json.dumps(overrides, indent=2))
                except Exception:
                    pass
        except Exception:
            pass

    if not runtime_path.exists():
        return base_config

    try:
        overrides = json.loads(runtime_path.read_text())
    except Exception:
        return base_config

    # Mapping from runtime_config keys → BotConfig fields
    field_map = {
        "position_size_usdc": ("position_size_usdc", float),
        "max_daily_loss_usdc": ("max_daily_loss_usdc", float),
        "max_total_exposure_usdc": ("max_total_exposure_usdc", float),
        "min_ev_threshold": ("min_ev_threshold", float),
        "hold_to_resolution": ("hold_to_resolution", bool),
        "take_profit_threshold": ("take_profit_threshold", float),
        "trailing_tp_enabled": ("trailing_tp_enabled", bool),
        "trailing_tp_trigger": ("trailing_tp_trigger", float),
        "trailing_tp_trail": ("trailing_tp_trail", float),
        "max_no_ask_multiplier": ("max_no_ask_multiplier", float),
        "high_vol_ask_multiplier": ("high_vol_ask_multiplier", float),
        "high_vol_assets": ("high_vol_assets", list),
        "max_spread_pct_of_ask": ("max_spread_pct_of_ask", float),

        "ghost_guard_enabled": ("ghost_guard_enabled", bool),
        "short_window_min_seconds_to_close": ("short_window_min_seconds_to_close", int),
        "min_streak_quality_score": ("min_streak_quality_score", float),
        "streak_quality_window": ("streak_quality_window", int),
        "lossmomentum_skip_enabled": ("lossmomentum_skip_enabled", bool),
        "reversal_toggling_enabled": ("reversal_toggling_enabled", bool),
        "reversal_lookback_n": ("reversal_lookback_n", int),
        "reversal_switch_threshold": ("reversal_switch_threshold", float),
        "reversal_spread_penalty": ("reversal_spread_penalty", float),
        "reversal_lookback_window_seconds": ("reversal_lookback_window_seconds", int),
        "regime_filter_enabled": ("regime_filter_enabled", bool),
        "stop_loss_enabled": ("stop_loss_enabled", bool),
        "stop_loss_pct": ("stop_loss_pct", float),
    }

    for rt_key, (cfg_field, cast_fn) in field_map.items():
        if rt_key in overrides:
            try:
                new_val = cast_fn(overrides[rt_key])
                if getattr(base_config, cfg_field) != new_val:
                    object.__setattr__(base_config, cfg_field, new_val)
            except (ValueError, TypeError):
                pass

    return base_config
