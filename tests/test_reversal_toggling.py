import asyncio
import os
import sys
import tempfile
import time
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market, create_position, close_position, get_position
from src.strategy.decision import DecisionEngine
from src.strategy.position_mgr import PositionManager


@pytest_asyncio.fixture
async def main_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _mock_config(**overrides):
    defaults = {
        "paper_trade": True,
        "bot_version": "v4.46",
        "position_size_usdc": 10.0,
        "max_open_positions": 5,
        "max_total_exposure_usdc": 100.0,
        "min_ev_threshold": 0.02,
        "global_cooldown": 0,
        "lossmomentum_skip_enabled": False,
        "max_no_ask_multiplier": 1.20,
        "high_vol_ask_multiplier": 1.15,
        "high_vol_assets": [],
        "target_market_mode": "crypto_short_window",
        "min_seconds_to_close_pre_entry": 180,
        "min_streak_quality_score": 0.45,
        "max_spread_pct_of_ask": 0.60,
        "reversal_toggling_enabled": True,
        "reversal_lookback_n": 10,
        "reversal_switch_threshold": 0.50,
        "reversal_spread_penalty": 0.01,
        "min_persistence_score": 0.55,
        "timeframe_15m_enabled": True,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


class TestReversalTogglingAndShadowPersistence:

    @pytest.mark.asyncio
    async def test_persistence_guard_rejects(self, main_db):
        """Verify that low persistence score blocks signal and returns False."""
        market_id = "0xshadow_persistence_market"
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        config = _mock_config(min_persistence_score=0.55)
        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock(return_value=True)

        validator = MagicMock()
        val_res = MagicMock()
        val_res.passed = True
        validator.validate = AsyncMock(return_value=val_res)

        clob = AsyncMock()
        clob.get_account_balance = AsyncMock(return_value={"balance": "100.0"})

        # Mock regime tracker to return a low persistence score (0.40)
        regime_tracker = MagicMock()
        regime_tracker.refresh_score = AsyncMock()
        regime_tracker.get_current_score = MagicMock(return_value=0.40)

        engine = DecisionEngine(
            config=config,
            validator=validator,
            position_mgr=pos_mgr,
            clob_client=clob,
            db=main_db,
            regime_tracker=regime_tracker
        )

        # process signal with side YES
        success, reason = await engine.process_signal(market, target_token_id="yes_tok_id", side="YES")

        # Must not be rejected (runs in shadow/warning mode for main bot)
        assert success is True
        pos_mgr.open_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_reversal_toggling_on_low_win_rate(self, main_db):
        """Verify that when win rate falls below 45%, the bot toggles to REVERSAL mode."""
        # 1. Insert completed positions with losses to force win rate to 0%
        # Let's insert 10 positions
        for i in range(10):
            m_id = f"0xcompleted_loss_m_{i}"
            market = {
                "market_id": m_id,
                "question": f"Will BTC resolve UP? {i}",
                "category": "Crypto",
                "yes_price": 0.50,
                "no_price": 0.50,
                "yes_token_id": f"yes_{i}",
                "no_token_id": f"no_{i}",
                "yes_volume_24h": 1000.0,
                "total_volume": 5000.0,
                "closes_at": int(time.time()) + 300,
                "last_scanned_at": int(time.time()),
                "is_flagged": 0,
                "flag_reason": None,
                "last_seen_above_threshold": None,
                "consecutive_cycles_above": 0,
            }
            await upsert_market(main_db, market)
            
            # Create completed loss position
            # Make the first trade notes TREND so it tracks as last completed strategy
            notes_str = json.dumps({"tid": f"no_{i}", "strategy": "TREND"})
            pos_id = await create_position(
                main_db,
                market_id=m_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.46",
                paper=True,
                notes=notes_str
            )
            # Close it as a loss
            await close_position(main_db, pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # 2. Setup decision engine on a new signal
        signal_market = {
            "market_id": "0xnew_signal_m",
            "question": "Will BTC resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "new_yes_tid",
            "no_token_id": "new_no_tid",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, signal_market)

        config = _mock_config(
            reversal_toggling_enabled=True,
            reversal_lookback_n=10,
            reversal_switch_threshold=0.50,
            reversal_spread_penalty=0.02
        )
        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock(return_value=True)

        validator = MagicMock()
        val_res = MagicMock()
        val_res.passed = True
        validator.validate = AsyncMock(return_value=val_res)

        clob = AsyncMock()
        clob.get_account_balance = AsyncMock(return_value={"balance": "100.0"})

        engine = DecisionEngine(
            config=config,
            validator=validator,
            position_mgr=pos_mgr,
            clob_client=clob,
            db=main_db
        )

        # Process signal: scanner side is YES (buying YES)
        # Since win rate of last 10 is 0% (< 50%), strategy must switch to REVERSAL.
        # Flipped side is NO, flipped token ID is 'new_no_tid'.
        # Flipped entry price = 1.0 - 0.50 (yes_price) + 0.02 (penalty) = 0.52.
        success, reason = await engine.process_signal(signal_market, target_token_id="new_yes_tid", side="YES")

        assert success is True
        pos_mgr.open_position.assert_called_once()
        
        # Check arguments passed to open_position
        kwargs = pos_mgr.open_position.call_args[1]
        assert kwargs["side"] == "NO"
        assert kwargs["target_token_id"] == "new_no_tid"
        assert kwargs["entry_price"] == 0.50
        assert kwargs["strategy_mode"] == "REVERSAL"

    @pytest.mark.asyncio
    async def test_consecutive_losses_override(self, main_db):
        """Verify that 5 consecutive losses force REVERSAL mode even if rolling win rate is above threshold."""
        now_ts = int(time.time())
        # 1. Insert 5 earlier wins (closed at time t=1)
        for i in range(5):
            m_id = f"0xwin_m_{i}"
            market = {
                "market_id": m_id,
                "question": f"Will BTC resolve UP? {i}",
                "category": "Crypto",
                "yes_price": 0.50,
                "no_price": 0.50,
                "yes_token_id": f"yes_{i}",
                "no_token_id": f"no_{i}",
                "yes_volume_24h": 1000.0,
                "total_volume": 5000.0,
                "closes_at": int(time.time()) + 300,
                "last_scanned_at": int(time.time()),
                "is_flagged": 0,
                "flag_reason": None,
                "last_seen_above_threshold": None,
                "consecutive_cycles_above": 0,
            }
            await upsert_market(main_db, market)
            
            notes_str = json.dumps({"tid": f"yes_{i}", "strategy": "TREND"})
            pos_id = await create_position(
                main_db,
                market_id=m_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.46",
                paper=True,
                notes=notes_str
            )
            # Closed as win
            await close_position(main_db, pos_id, exit_price=1.0, realised_pnl=10.0, close_reason="RESOLUTION", paper=True)
            # Override closed_at to make wins older (t = now_ts - 10 to -6)
            await main_db.execute("UPDATE paper_positions SET closed_at = ? WHERE position_id = ?", (now_ts - 10 + i, pos_id))
            await main_db.commit()

        # 2. Insert 5 more recent losses (closed at time t=2)
        for i in range(5, 10):
            m_id = f"0xloss_m_{i}"
            market = {
                "market_id": m_id,
                "question": f"Will BTC resolve UP? {i}",
                "category": "Crypto",
                "yes_price": 0.50,
                "no_price": 0.50,
                "yes_token_id": f"yes_{i}",
                "no_token_id": f"no_{i}",
                "yes_volume_24h": 1000.0,
                "total_volume": 5000.0,
                "closes_at": int(time.time()) + 300,
                "last_scanned_at": int(time.time()),
                "is_flagged": 0,
                "flag_reason": None,
                "last_seen_above_threshold": None,
                "consecutive_cycles_above": 0,
            }
            await upsert_market(main_db, market)
            
            notes_str = json.dumps({"tid": f"yes_{i}", "strategy": "TREND"})
            pos_id = await create_position(
                main_db,
                market_id=m_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.46",
                paper=True,
                notes=notes_str
            )
            # Closed as loss
            await close_position(main_db, pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)
            # Override closed_at to make losses newer (t = now_ts - 5 to -1)
            await main_db.execute("UPDATE paper_positions SET closed_at = ? WHERE position_id = ?", (now_ts - 5 + (i - 5), pos_id))
            await main_db.commit()

        # 3. Setup decision engine on a new signal
        signal_market = {
            "market_id": "0xnew_signal_m_consecutive",
            "question": "Will BTC resolve UP? (Signal consecutive)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "new_yes_tid_c",
            "no_token_id": "new_no_tid_c",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, signal_market)

        # Lookback=10, Win rate = 5 / 10 = 50%. Switch threshold = 0.40.
        # Normally 50% >= 40% would retain TREND.
        # But consecutive losses of the last 5 (index 5 to 9) is 100%.
        # So it should force REVERSAL.
        config = _mock_config(
            reversal_toggling_enabled=True,
            reversal_lookback_n=10,
            reversal_switch_threshold=0.40,
            reversal_spread_penalty=0.02
        )
        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock(return_value=True)

        validator = MagicMock()
        val_res = MagicMock()
        val_res.passed = True
        validator.validate = AsyncMock(return_value=val_res)

        clob = AsyncMock()
        clob.get_account_balance = AsyncMock(return_value={"balance": "100.0"})

        engine = DecisionEngine(
            config=config,
            validator=validator,
            position_mgr=pos_mgr,
            clob_client=clob,
            db=main_db
        )

        success, reason = await engine.process_signal(signal_market, target_token_id="new_yes_tid_c", side="YES")

        assert success is True
        pos_mgr.open_position.assert_called_once()
        
        kwargs = pos_mgr.open_position.call_args[1]
        assert kwargs["side"] == "NO"
        assert kwargs["strategy_mode"] == "REVERSAL"



class TestPositionManagerTokenHealingJSON:

    @pytest.mark.asyncio
    async def test_recover_target_token_id_preserves_notes_json(self, main_db):
        """Verify that _recover_target_token_id heals the token ID while preserving JSON structure."""
        market_id = "0xheal_market"
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        # Insert position with valid notes JSON having strategy mode REVERSAL
        original_notes = json.dumps({"tid": "dummy_old_tid", "strategy": "REVERSAL"})
        pos_id = await create_position(
            main_db,
            market_id=market_id,
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.46",
            paper=True,
            notes=original_notes
        )

        # Set up PositionManager and mock gamma client
        config = _mock_config()
        clob = AsyncMock()
        gamma = AsyncMock()
        gamma.get_market_info = AsyncMock(return_value={
            "tokens": [
                {"outcome": "Yes", "token_id": "yes_tok_id"},
                {"outcome": "No", "token_id": "no_tok_id"}
            ]
        })

        mgr = PositionManager(config, clob, main_db, gamma_client=gamma)

        # Call recover for side NO
        recovered_tid = await mgr._recover_target_token_id(market_id, pos_id, side="NO")
        assert recovered_tid == "no_tok_id"

        # Check DB to verify notes payload was updated correctly
        pos = await get_position(main_db, pos_id, paper=True)
        notes_val = pos["notes"]
        
        assert notes_val.startswith("{")
        data = json.loads(notes_val)
        assert data["tid"] == "no_tok_id"
        assert data["strategy"] == "REVERSAL"

    @pytest.mark.asyncio
    async def test_persist_tracking_data_preserves_notes_json(self, main_db):
        """Verify that _persist_tracking_data saves tracking data while preserving existing notes keys like strategy."""
        market_id = "0xpersist_market"
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        original_notes = json.dumps({"tid": "yes_tok_id", "strategy": "REVERSAL"})
        pos_id = await create_position(
            main_db,
            market_id=market_id,
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.46",
            paper=True,
            notes=original_notes
        )

        config = _mock_config()
        clob = AsyncMock()
        mgr = PositionManager(config, clob, main_db)
        
        # Populate in-memory state
        mgr._partial_proceeds[pos_id] = 2.5
        mgr._shares_remaining[pos_id] = 17.5
        mgr._resting_tp_orders[pos_id] = [{"order_id": "resting_order_123"}]

        # Call _persist_tracking_data
        await mgr._persist_tracking_data(pos_id, "yes_tok_id")

        # Check DB to verify notes payload was merged
        pos = await get_position(main_db, pos_id, paper=True)
        notes_val = pos["notes"]
        
        assert notes_val.startswith("{")
        data = json.loads(notes_val)
        assert data["tid"] == "yes_tok_id"
        assert data["strategy"] == "REVERSAL"
        assert data["p"] == 2.5
        assert data["r"] == 17.5
        assert data["orders"] == [{"order_id": "resting_order_123"}]

    @pytest.mark.asyncio
    async def test_closed_at_uses_market_closes_at_for_resolution(self, main_db):
        """Verify that close_position sets closed_at to the market's closes_at for RESOLUTION close reason."""
        market_id = "0xcloses_at_market"
        market_closes_at = int(time.time()) - 100000  # More than 24 hours ago
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": market_closes_at,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        pos_id = await create_position(
            main_db,
            market_id=market_id,
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.80",
            paper=True
        )

        # Close via RESOLUTION
        await close_position(main_db, pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # Assert closed_at matches market_closes_at
        pos = await get_position(main_db, pos_id, paper=True)
        assert pos["closed_at"] == market_closes_at

        # Close another via STOP_LOSS
        pos_id2 = await create_position(
            main_db,
            market_id=market_id,
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.80",
            paper=True
        )
        await close_position(main_db, pos_id2, exit_price=0.0, realised_pnl=-10.0, close_reason="STOP_LOSS", paper=True)
        pos2 = await get_position(main_db, pos_id2, paper=True)
        assert pos2["closed_at"] > market_closes_at  # Should be current time 'now'

    @pytest.mark.asyncio
    async def test_reversal_mode_ignores_stale_losses(self, main_db):
        """Verify that _get_active_strategy_mode ignores completed trades older than the lookback window."""
        market_id = "0xstale_market"
        stale_closed_at = int(time.time()) - 100000  # More than 24 hours ago
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": stale_closed_at,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        # Write 5 losses that are older than 24 hours
        for i in range(5):
            pos_id = await create_position(
                main_db,
                market_id=market_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.80",
                paper=True,
                notes=json.dumps({"strategy": "TREND"})
            )
            # Use raw SQL to force closed_at to stale time, since close_position would use market closes_at
            await main_db.execute(
                "UPDATE paper_positions SET order_status = 'FILLED', closed_at = ?, exit_price = 0.0, realised_pnl = -10.0, close_reason = 'STOP_LOSS' WHERE position_id = ?",
                (stale_closed_at, pos_id)
            )
            await main_db.commit()

        config = _mock_config(reversal_lookback_window_seconds=86400, reversal_lookback_n=10)
        pos_mgr = MagicMock()
        validator = MagicMock()
        clob = AsyncMock()
        engine = DecisionEngine(
            config=config,
            validator=validator,
            position_mgr=pos_mgr,
            clob_client=clob,
            db=main_db
        )

        # Evaluate active strategy mode - should return TREND since stale losses are ignored
        mode = await engine._get_active_strategy_mode()
        assert mode == "TREND"

        # Now write 5 recent losses
        recent_closed_at = int(time.time()) - 3600  # 1 hour ago
        for i in range(5):
            pos_id = await create_position(
                main_db,
                market_id=market_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.80",
                paper=True,
                notes=json.dumps({"strategy": "TREND"})
            )
            await main_db.execute(
                "UPDATE paper_positions SET order_status = 'FILLED', closed_at = ?, exit_price = 0.0, realised_pnl = -10.0, close_reason = 'STOP_LOSS' WHERE position_id = ?",
                (recent_closed_at, pos_id)
            )
            await main_db.commit()

        # Should toggle to REVERSAL now
        mode = await engine._get_active_strategy_mode()
        assert mode == "REVERSAL"

    @pytest.mark.asyncio
    async def test_regime_filter_circuit_breaker(self, main_db):
        """Verify that regime filter correctly blocks TREND trades in range markets and REVERSAL trades in trending markets."""
        # Setup market
        market_id = "0xregime_market"
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_id",
            "no_token_id": "no_tok_id",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 500,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        config = _mock_config(regime_filter_enabled=True, reversal_toggling_enabled=False)
        pos_mgr = MagicMock()
        pos_mgr.open_position = AsyncMock(return_value=True)
        # Mock WebSocket ask to bypass Best Ask exceptions
        pos_mgr.ws = MagicMock()
        pos_mgr.ws.get_best_ask.return_value = 0.50
        validator = MagicMock()
        val_res = MagicMock()
        val_res.passed = True
        validator.validate = AsyncMock(return_value=val_res)
        clob = AsyncMock()
        
        engine = DecisionEngine(
            config=config,
            validator=validator,
            position_mgr=pos_mgr,
            clob_client=clob,
            db=main_db
        )
        
        # 1. First scenario: Choppy Regime (reversal_rolling_pnl > 0)
        # Write 1 virtual reversal win
        await main_db.execute(
            "INSERT INTO profile_positions (position_id, market_id, side, order_status, profile_name, realised_pnl, closed_at) "
            "VALUES ('pos_r1', ?, 'YES', 'FILLED', 'reversal', 10.0, ?)",
            (market_id, int(time.time()) - 100)
        )
        await main_db.commit()

        # With toggling disabled, active_strategy mode is TREND.
        # But we are in a Choppy Regime (PnL = +10.0 > 0), so TREND trade should be BLOCKED.
        success, reason = await engine.process_signal(market, side="YES")
        assert success is False
        assert "choppy regime" in reason.lower()

        # 2. Second scenario: Trending Regime (reversal_rolling_pnl <= 0)
        # Clear profile positions and insert a loss
        await main_db.execute("DELETE FROM profile_positions")
        await main_db.execute(
            "INSERT INTO profile_positions (position_id, market_id, side, order_status, profile_name, realised_pnl, closed_at) "
            "VALUES ('pos_r2', ?, 'YES', 'FILLED', 'reversal', -5.0, ?)",
            (market_id, int(time.time()) - 100)
        )
        await main_db.commit()

        # Force active_strategy mode to REVERSAL by overriding reversal_toggling_enabled=True and inserting recent losses to force REVERSAL
        config = _mock_config(regime_filter_enabled=True, reversal_toggling_enabled=True, reversal_lookback_n=10)
        engine.config = config
        
        # Insert 5 recent losses in paper_positions to trigger REVERSAL mode
        recent_closed_at = int(time.time()) - 3600
        for i in range(5):
            pos_id = await create_position(
                main_db,
                market_id=market_id,
                entry_price=0.50,
                size_usdc=10.0,
                shares_bought=20,
                bot_version="v4.80",
                paper=True,
                notes=json.dumps({"strategy": "TREND"})
            )
            await main_db.execute(
                "UPDATE paper_positions SET order_status = 'FILLED', closed_at = ?, exit_price = 0.0, realised_pnl = -10.0, close_reason = 'STOP_LOSS' WHERE position_id = ?",
                (recent_closed_at, pos_id)
            )
            await main_db.commit()

        # Check strategy mode: should be TREND due to deadlock override
        mode = await engine._get_active_strategy_mode()
        assert mode == "TREND"

        # Create a new market for the second signal to avoid duplicate check block
        market2_id = "0xregime_market_2"
        market2 = dict(market)
        market2["market_id"] = market2_id
        await upsert_market(main_db, market2)

        # Trending Regime (PnL = -5.0 <= 0), so TREND trade is ALLOWED (no deadlock)
        success, reason = await engine.process_signal(market2, side="YES")
        assert success is True


