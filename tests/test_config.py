import os
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from config.config import load_runtime_overrides, BotConfig

def test_load_runtime_overrides_self_healing_override():
    # Create temp files for .env.rd and runtime_config_rd.json
    with tempfile.TemporaryDirectory() as tmpdir:
        # Patch the paths inside config.py to use our temp files
        runtime_path = Path(tmpdir) / "runtime_config_rd.json"
        env_path = Path(tmpdir) / "env.rd"

        # 1. Write the runtime config (older)
        runtime_path.write_text(json.dumps({
            "stop_loss_enabled": False,
            "stop_loss_pct": 0.12,
            "position_size_usdc": 10.0
        }))
        
        # Set runtime config mtime to past
        past_time = time.time() - 10.0
        os.utime(str(runtime_path), (past_time, past_time))

        # 2. Write the env file (newer)
        env_path.write_text(
            "STOP_LOSS_ENABLED=true\n"
            "STOP_LOSS_PCT=0.15\n"
        )
        # Set env config mtime to present (newer than runtime config)
        present_time = time.time()
        os.utime(str(env_path), (present_time, present_time))

        # Create a mock base config
        base_config = BotConfig(
            private_key="test",
            poly_api_key="test",
            poly_api_secret="test",
            poly_passphrase="test",
            chain_id=137,
            clob_api_url="test",
            gamma_api_url="test",
            polygon_rpc="test",
            position_size_usdc=2.0,  # Defined in base (should stay 10.0 if not in env)
            max_daily_loss_usdc=10.0,
            max_total_exposure_usdc=20.0,
            min_ev_threshold=0.02,
            hold_to_resolution=False,
            take_profit_threshold=0.95,
            trailing_tp_enabled=False,
            trailing_tp_trigger=0.90,
            trailing_tp_trail=0.05,
            max_no_ask_multiplier=1.2,
            high_vol_ask_multiplier=1.15,
            high_vol_assets=["BTC"],
            max_spread_pct_of_ask=0.6,
            ghost_guard_enabled=False,
            rate_limit_orders_per_min=50,
            rate_limit_api_calls_per_min=100,
            paper_trade=True,
            bot_version="v0.0",
            dashboard_password="test",
            stop_loss_enabled=True,  # Base config has true
            stop_loss_pct=0.15,       # Base config has 0.15
            target_market_mode="crypto_short_window"
        )

        # Mock the Path lookups in load_runtime_overrides
        with patch("config.config.Path") as mock_path:
            def side_effect(path_str):
                if "runtime_config_rd.json" in path_str:
                    return runtime_path
                if "env.rd" in path_str:
                    return env_path
                return Path(path_str)
            mock_path.side_effect = side_effect

            # Execute load_runtime_overrides
            final_cfg = load_runtime_overrides(base_config)

            # Check that stop_loss_enabled and stop_loss_pct were reverted/popped
            assert final_cfg.stop_loss_enabled is True
            assert final_cfg.stop_loss_pct == 0.15
            # position_size_usdc was NOT in env, so it should stay overridden by the runtime config
            assert final_cfg.position_size_usdc == 10.0

            # Verify that the runtime_config_rd.json file on disk was modified and keys popped
            updated_overrides = json.loads(runtime_path.read_text())
            assert "stop_loss_enabled" not in updated_overrides
            assert "stop_loss_pct" not in updated_overrides
            assert updated_overrides["position_size_usdc"] == 10.0
