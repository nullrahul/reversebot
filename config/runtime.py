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
from typing import Any, Dict, Optional

from config.config import BotConfig


# Fields that can be hot-reloaded from the dashboard
_HOT_FIELDS: Dict[str, type] = {
    "position_size_usdc": float,
    "max_daily_loss_usdc": float,
    "max_total_exposure_usdc": float,
    "min_ev_threshold": float,
    "hold_to_resolution": bool,
    "max_no_ask_multiplier": float,
    "high_vol_ask_multiplier": float,
    "high_vol_assets": list,
    "max_spread_pct_of_ask": float,
    "ghost_guard_enabled": bool,
    "short_window_min_seconds_to_close": int,
    # Extended hot fields used by decision/validator (via getattr with defaults)
    "min_seconds_to_close_pre_entry": int,
    "price_velocity_max": float,
    "price_velocity_window": int,
    "_TESTING_only_15min_windows": bool,
    "early_phase_min_seconds": int,
    "early_yes_bias_max": float,
    "mid_phase_min_seconds": int,
    "mid_yes_bias_max": float,
    "late_yes_bias_max": float,
    "min_entry_price": float,
    "max_entry_price": float,
    "min_streak_quality_score": float,
    "streak_quality_window": int,
    "lossmomentum_skip_enabled": bool,
    "min_persistence_score": float,
    "reversal_toggling_enabled": bool,
    "reversal_lookback_n": int,
    "reversal_switch_threshold": float,
    "reversal_spread_penalty": float,
    "reversal_lookback_window_seconds": int,
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

        # Defaults for hot-fields that may not yet exist in the base BotConfig
        _FIELD_DEFAULTS: Dict[str, Any] = {
            "min_seconds_to_close_pre_entry": 60,
            "price_velocity_max": 0.025,
            "price_velocity_window": 5,
            "_TESTING_only_15min_windows": False,
            "early_phase_min_seconds": 43200,
            "early_yes_bias_max": 0.85,
            "mid_phase_min_seconds": 14400,
            "mid_yes_bias_max": 0.80,
            "late_yes_bias_max": 0.75,
            "min_entry_price": 0.30,
            "max_entry_price": 0.60,
            "min_streak_quality_score": 0.45,
            "streak_quality_window": 7,
            "lossmomentum_skip_enabled": True,
            "min_persistence_score": 0.55,
            "reversal_toggling_enabled": False,
            "reversal_lookback_n": 10,
            "reversal_switch_threshold": 0.50,
            "reversal_spread_penalty": 0.0,
            "reversal_lookback_window_seconds": 86400,
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
