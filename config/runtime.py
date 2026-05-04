"""
RuntimeConfig — Mutable shared configuration for hot-reload support.

Problem: BotConfig is frozen (immutable). When load_runtime_overrides()
creates a new config, sub-engines (Scanner, Decision, PositionMgr, Validator)
still hold stale snapshots from boot time.

Solution: This wrapper holds mutable references to hot-reloadable fields.
All sub-engines receive a REFERENCE to the same RuntimeConfig instance.
When the dashboard changes a parameter, reload_from_disk() updates the
shared instance — every engine sees the change instantly.

Immutable fields (API keys, auth, paths) delegate to the base BotConfig.
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.config import BotConfig


# Fields that can be hot-reloaded from the dashboard
_HOT_FIELDS: Dict[str, type] = {
    "yes_bias_threshold": float,
    "yes_bias_max_threshold": float,
    "min_volume_24h_usdc": float,
    "min_time_to_close_hours": float,
    "max_time_to_close_days": float,
    "position_size_usdc": float,
    "max_open_positions": int,
    "max_daily_loss_usdc": float,
    "max_total_exposure_usdc": float,
    "take_profit_multiplier": float,
    "stop_loss_multiplier": float,
    "max_no_ask_multiplier": float,
    "high_vol_ask_multiplier": float,
    "high_vol_assets": list,
    "trailing_tp_activation_multiplier": float,
    "trailing_tp_buffer_pct": float,
    "min_ev_threshold": float,
    "max_spread_pct_of_ask": float,
    "correlated_window_seconds": int,
    "max_same_direction_entries": int,
    "hold_to_resolution": bool,
    "poll_interval_seconds": int,
    "global_cooldown": int,
    "slippage_depth_multiplier": float,
    "short_window_min_seconds_to_close": int,
    "trailing_tp_enabled": bool,
    "tp1_shares_count": int,
    "tp1_multiplier": float,
    "tp2_multiplier": float,
    # ── Quality Filter (Change 4 + 5 + 7 from strategy_diagnosis.md) ──
    "min_consecutive_cycles": int,      # Min ticks YES must stay in zone before entry
    "min_seconds_to_close_pre_entry": int,  # Reject entry if < N secs left on market
    "price_velocity_max": float,        # Max YES price velocity allowed (anti-momentum)
    "price_velocity_window": int,       # Number of price ticks to measure velocity over
    # ── Testing Flags ──
    "_TESTING_only_15min_windows": bool,  # TEMP: Ignore 5-min windows, subscribe 15-min only
}


class RuntimeConfig:
    """
    Thread-safe mutable config wrapper.

    All sub-engines hold a reference to the SAME RuntimeConfig instance.
    Dashboard writes update all consumers instantly via reload_from_disk().

    Attribute access falls through to the base BotConfig for immutable fields
    (API keys, auth credentials, paths, etc.).
    """

    def __init__(self, base: BotConfig) -> None:
        self._base = base
        self._lock = threading.Lock()

        # Determine runtime config path based on market mode
        if getattr(base, "target_market_mode", "") == "crypto_short_window":
            self._runtime_path = Path("dashboard/runtime_config_rd.json")
        else:
            self._runtime_path = Path("dashboard/runtime_config.json")

        # Defaults for new hot-fields that may not yet exist in the base BotConfig
        _FIELD_DEFAULTS: Dict[str, Any] = {
            "min_consecutive_cycles": 8,
            "min_seconds_to_close_pre_entry": 180,
            "price_velocity_max": 0.025,
            "price_velocity_window": 5,
            "_TESTING_only_15min_windows": False,  # Off by default; enable via JSON
        }

        # Initialize all hot-reloadable fields from base config (or defaults)
        for field_name, cast_fn in _HOT_FIELDS.items():
            if hasattr(base, field_name):
                setattr(self, field_name, getattr(base, field_name))
            else:
                setattr(self, field_name, _FIELD_DEFAULTS.get(field_name, cast_fn()))

    def reload_from_disk(self) -> bool:
        """
        Read runtime_config JSON and update hot-reloadable fields in-place.
        Returns True if any field was changed.

        Thread-safe: uses a lock to prevent partial reads during writes.
        """
        if not self._runtime_path.exists():
            return False

        try:
            raw = self._runtime_path.read_text()
            overrides: Dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return False

        changed = False
        with self._lock:
            for field_name, cast_fn in _HOT_FIELDS.items():
                if field_name in overrides:
                    try:
                        new_val = cast_fn(overrides[field_name])
                        if getattr(self, field_name) != new_val:
                            setattr(self, field_name, new_val)
                            changed = True
                    except (ValueError, TypeError):
                        pass

        return changed

    def __getattr__(self, name: str) -> Any:
        """Delegate immutable fields to the base BotConfig."""
        # This is only called when normal attribute lookup fails
        # (i.e., the field is NOT a hot-reloadable field set via __init__)
        try:
            return getattr(self._base, name)
        except AttributeError:
            raise AttributeError(
                f"'{type(self).__name__}' has no attribute '{name}'"
            )

    def __repr__(self) -> str:
        hot = {f: getattr(self, f) for f in _HOT_FIELDS}
        return f"RuntimeConfig(mode={self._base.target_market_mode}, hot={hot})"
