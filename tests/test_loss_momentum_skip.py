import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market, create_position, close_position, get_last_closed_trend_trade
from src.db.skip_db import LossMomentumSkipDb
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


@pytest_asyncio.fixture
async def skip_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = LossMomentumSkipDb(tmp.name)
    await db_conn.init()
    yield db_conn
    await db_conn.close()
    os.unlink(tmp.name)


def _mock_config(**overrides):
    defaults = {
        "paper_trade": True,
        "bot_version": "v0.1.0",
        "position_size_usdc": 10.0,
        "max_open_positions": 5,
        "max_total_exposure_usdc": 100.0,
        "min_ev_threshold": 0.02,
        "global_cooldown": 0,  # 0 to avoid cooldown rejections in tests
        "lossmomentum_skip_enabled": False,
        "max_no_ask_multiplier": 1.20,
        "high_vol_ask_multiplier": 1.15,
        "high_vol_assets": [],
        "target_market_mode": "crypto_short_window",
        "min_seconds_to_close_pre_entry": 180,
        "min_streak_quality_score": 0.45,
        "max_spread_pct_of_ask": 0.60,
        "reversal_spread_penalty": 0.01,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


class TestLossMomentumSkipDb:

    @pytest.mark.asyncio
    async def test_record_and_check_skip(self, skip_db):
        """Test recording skipped positions and checking trigger IDs."""
        market_id = "market_123"
        question = "Will BTC resolve UP?"
        side = "NO"
        entry_price = 0.50
        size_usdc = 10.0
        shares_bought = 20.0
        trigger_id = "loss_trigger_uuid"

        # Record a skip
        skipped_id = await skip_db.record_skip(
            market_id=market_id,
            question=question,
            side=side,
            entry_price=entry_price,
            size_usdc=size_usdc,
            shares_bought=shares_bought,
            trigger_position_id=trigger_id,
            symbol="BTC"
        )

        assert skipped_id is not None
        assert len(skipped_id) > 0

        # Check has_skipped_for_trigger
        assert await skip_db.has_skipped_for_trigger(trigger_id) is True
        assert await skip_db.has_skipped_for_trigger("non_existent_trigger") is False

        # Get unresolved skips
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 1
        assert unresolved[0]["position_id"] == skipped_id
        assert unresolved[0]["status"] == "SKIPPED"
        assert unresolved[0]["trigger_position_id"] == trigger_id

        # Resolve the skip (WIN)
        # 20.0 shares * 1.0 = 20.0 gross value. Net realized pnl = 20.0 - 10.0 = 10.0
        await skip_db.resolve_skip(skipped_id, exit_price=1.0, realised_pnl=10.0)

        # Verify unresolved is now empty
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 0


class TestDecisionEngineSkipCheck:

    @pytest.mark.asyncio
    async def test_soft_disabled_mode_records_but_does_not_block(self, main_db, skip_db):
        """Under soft-disabled mode, skipped trades are recorded but not blocked."""
        # 1. Insert a trigger market and a new signal market
        trigger_market = {
            "market_id": "0xtest_soft_trigger",
            "question": "Will BTC resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trigger_market)

        signal_market = {
            "market_id": "0xtest_soft_signal",
            "question": "Will BTC resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
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

        # 2. Insert a closed LOSS position in main_db on the trigger market
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_soft_trigger",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # Setup Config, Position Manager, and Decision Engine
        config = _mock_config(lossmomentum_skip_enabled=False)
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
            db=main_db,
            skip_db=skip_db
        )

        # Run process_signal on the new signal market
        success, reason = await engine.process_signal(signal_market, target_token_id="no_token_id", side="NO")

        # Soft-disabled strategy should let trade proceed
        assert success is True
        assert reason == "Order placed successfully"
        pos_mgr.open_position.assert_called_once()

        # It should have recorded the skip in skip_db
        assert await skip_db.has_skipped_for_trigger(loss_pos_id) is True
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 1
        assert unresolved[0]["trigger_position_id"] == loss_pos_id
        assert unresolved[0]["market_id"] == "0xtest_soft_signal"

    @pytest.mark.asyncio
    async def test_hard_enabled_mode_records_and_fades(self, main_db, skip_db):
        """Under hard-enabled mode, skipped trades are recorded AND faded (reversed)."""
        # 1. Insert trigger and signal markets
        trigger_market = {
            "market_id": "0xtest_hard_trigger",
            "question": "Will ETH resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_hard_trig",
            "no_token_id": "no_tok_hard_trig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trigger_market)

        signal_market = {
            "market_id": "0xtest_hard_signal",
            "question": "Will ETH resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_token_id": "yes_tok_hard_sig",
            "no_token_id": "no_tok_hard_sig",
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

        # 2. Insert a closed LOSS position in main_db
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_hard_trigger",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # Setup Config, Position Manager, and Decision Engine
        config = _mock_config(lossmomentum_skip_enabled=True, reversal_spread_penalty=0.02)
        pos_mgr = MagicMock()
        pos_mgr.config = config
        pos_mgr._paper = True
        pos_mgr.open_position = AsyncMock(return_value=True)
        pos_mgr.open_profile_position = AsyncMock()

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
            db=main_db,
            skip_db=skip_db
        )

        # Run process_signal
        success, reason = await engine.process_signal(signal_market, target_token_id="no_tok_hard_sig", side="NO")

        # Hard-enabled strategy should execute trade on the reversed side
        assert success is True
        assert reason == "Order placed successfully"
        
        # Verify that open_position was called with reversed side "YES" and correct token and price (0.45)
        pos_mgr.open_position.assert_called_once()
        call_kwargs = pos_mgr.open_position.call_args[1]
        assert call_kwargs["side"] == "YES"
        assert call_kwargs["target_token_id"] == "yes_tok_hard_sig"
        assert abs(call_kwargs["entry_price"] - 0.45) < 0.001
        assert call_kwargs["strategy_mode"] == "LOSS_FADE"

        # It should have recorded the skip in skip_db using original trend side "NO" and price 0.55
        assert await skip_db.has_skipped_for_trigger(loss_pos_id) is True
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 1
        assert unresolved[0]["side"] == "NO"
        assert unresolved[0]["entry_price"] == 0.55

        # Verify that virtual loss_skip profile opened a position on the reversal side "YES" and price 0.45
        profile_calls = {call.kwargs.get("profile_name"): call.kwargs for call in pos_mgr.open_profile_position.call_args_list}
        assert "loss_skip" in profile_calls
        loss_skip_call = profile_calls["loss_skip"]
        assert loss_skip_call["side"] == "YES"
        assert abs(loss_skip_call["entry_price"] - 0.45) < 0.001
        assert loss_skip_call["target_token_id"] == "yes_tok_hard_sig"
        assert loss_skip_call["strategy_mode"] == "LOSS_FADE"

    @pytest.mark.asyncio
    async def test_hard_enabled_mode_blocks_high_price_fades(self, main_db, skip_db):
        """Under hard-enabled mode, if the opposing price is >= 0.50, the fade is blocked and the trade is skipped."""
        # 1. Insert trigger and signal markets
        trigger_market = {
            "market_id": "0xtest_hard_block_trigger",
            "question": "Will ETH resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_hard_block_trig",
            "no_token_id": "no_tok_hard_block_trig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trigger_market)

        signal_market = {
            "market_id": "0xtest_hard_block_signal",
            "question": "Will ETH resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50, # Reversal price >= 0.50
            "no_price": 0.50,
            "yes_token_id": "yes_tok_hard_block_sig",
            "no_token_id": "no_tok_hard_block_sig",
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

        # 2. Insert a closed LOSS position in main_db
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_hard_block_trigger",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # Setup Config, Position Manager, and Decision Engine
        config = _mock_config(lossmomentum_skip_enabled=True, reversal_spread_penalty=0.02)
        pos_mgr = MagicMock()
        pos_mgr.config = config
        pos_mgr._paper = True
        pos_mgr.open_position = AsyncMock(return_value=True)
        pos_mgr.open_profile_position = AsyncMock()

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
            db=main_db,
            skip_db=skip_db
        )

        # Run process_signal
        success, reason = await engine.process_signal(signal_market, target_token_id="no_tok_hard_block_sig", side="NO")

        # Reversal price is 0.50, which is >= 0.50, so it should be blocked/skipped
        assert success is False
        assert "Blocked by Loss Momentum Skip Strategy" in reason
        
        # open_position should NOT have been called
        pos_mgr.open_position.assert_not_called()

        # It should have recorded the skip in skip_db
        assert await skip_db.has_skipped_for_trigger(loss_pos_id) is True
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 1
        assert unresolved[0]["side"] == "NO"
        assert unresolved[0]["entry_price"] == 0.50

        # Verify that virtual loss_skip profile also blocked the trade (open_profile_position not called)
        profile_calls = {call.kwargs.get("profile_name"): call.kwargs for call in pos_mgr.open_profile_position.call_args_list}
        assert "loss_skip" not in profile_calls

    @pytest.mark.asyncio
    async def test_no_skip_when_last_trade_is_win(self, main_db, skip_db):
        """If the last closed position was a WIN, we do not skip/record."""
        trigger_market = {
            "market_id": "0xtest_win_trigger",
            "question": "Will BTC resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trigger_market)

        signal_market = {
            "market_id": "0xtest_win_signal",
            "question": "Will BTC resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
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

        # Insert a closed WIN position in main_db
        win_pos_id = await create_position(
            main_db,
            market_id="0xtest_win_trigger",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id"
        )
        await close_position(main_db, win_pos_id, exit_price=1.0, realised_pnl=10.0, close_reason="RESOLUTION", paper=True)

        config = _mock_config(lossmomentum_skip_enabled=True)  # Set hard enabled to verify it doesn't block
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
            db=main_db,
            skip_db=skip_db
        )

        success, reason = await engine.process_signal(signal_market, target_token_id="no_token_id", side="NO")

        # Should allow execution because last trade was a WIN
        assert success is True
        pos_mgr.open_position.assert_called_once()
        assert await skip_db.has_skipped_for_trigger(win_pos_id) is False


class TestPositionManagerSkipResolution:

    @pytest.mark.asyncio
    async def test_resolve_skipped_trade_via_binance(self, main_db, skip_db):
        """Verify that PositionManager resolves skipped positions using Binance resolution outcomes."""
        market_id = "0xskip_res"
        market = {
            "market_id": market_id,
            "question": "Will BTC resolve UP?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) - 100,  # Expired
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, market)

        # Record a skipped trade
        skipped_id = await skip_db.record_skip(
            market_id=market_id,
            question="Will BTC resolve UP?",
            side="NO",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20.0,
            trigger_position_id="some_trigger_uuid",
            symbol="BTC"
        )

        config = _mock_config()
        clob = AsyncMock()
        mgr = PositionManager(config, clob, main_db, skip_db=skip_db)

        # Mock Binance resolution client call to return DOWN (which means NO wins)
        mgr._get_binance_resolution = AsyncMock(return_value="DOWN")

        # Run resolution check
        await mgr.check_resolutions()

        # Skip should be resolved now (unresolved list empty)
        unresolved = await skip_db.get_unresolved_skips()
        assert len(unresolved) == 0

        # Query resolved data from skip_db
        cursor = await skip_db._db.execute("SELECT * FROM skipped_positions WHERE position_id = ?", (skipped_id,))
        pos = await cursor.fetchone()
        assert pos is not None
        assert pos["status"] == "RESOLVED"
        assert pos["exit_price"] == 1.0
        assert pos["realised_pnl"] == 10.0  # 20 shares * 1.0 - 10.0 size


class TestLossFadeConcurrencyAndDecoupling:

    @pytest.mark.asyncio
    async def test_loss_fade_ignores_completed_fade_wins_under_decoupling(self, main_db, skip_db):
        """Verify get_last_closed_trend_trade ignores LOSS_FADE trades in positions/paper_positions."""
        # Upsert markets
        trig_market = {
            "market_id": "0xtest_decouple_trig",
            "question": "ETH UP?",
            "category": "Crypto",
            "closes_at": int(time.time()) + 300,
        }
        fade_market = {
            "market_id": "0xtest_decouple_fade",
            "question": "ETH UP?",
            "category": "Crypto",
            "closes_at": int(time.time()) + 300,
        }
        await upsert_market(main_db, trig_market)
        await upsert_market(main_db, fade_market)

        # 1. Insert a LOSS position in main_db
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_decouple_trig",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # 2. Insert a WIN position with strategy_mode = 'LOSS_FADE' in main_db
        fade_pos_id = await create_position(
            main_db,
            market_id="0xtest_decouple_fade",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="LOSS_FADE"
        )
        await close_position(main_db, fade_pos_id, exit_price=1.0, realised_pnl=10.0, close_reason="RESOLUTION", paper=True)

        # 3. Call get_last_closed_trend_trade. It should return the loss_pos_id (excluding the LOSS_FADE win).
        last_closed = await get_last_closed_trend_trade(main_db, skip_db, paper=True)
        assert last_closed is not None
        assert last_closed["position_id"] == loss_pos_id
        assert last_closed["realised_pnl"] == -10.0

    @pytest.mark.asyncio
    async def test_loss_fade_concurrency_blocks_new_fade_when_active(self, main_db, skip_db):
        """Verify that DecisionEngine blocks new signal when a LOSS_FADE trade is already active (OPEN)."""
        # 1. Insert markets
        trig_market = {
            "market_id": "0xtest_fade_trig",
            "question": "Will ETH resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_trig",
            "no_token_id": "no_tok_trig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        active_fade_market = {
            "market_id": "0xtest_active_fade",
            "question": "Will ETH resolve UP? (Active)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_active",
            "no_token_id": "no_tok_active",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        signal_market = {
            "market_id": "0xtest_fade_sig",
            "question": "Will ETH resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_sig",
            "no_token_id": "no_tok_sig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trig_market)
        await upsert_market(main_db, active_fade_market)
        await upsert_market(main_db, signal_market)

        # 2. Insert a closed LOSS position in main_db to trigger recovery/skip mode
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_fade_trig",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # 3. Insert an active (OPEN) LOSS_FADE trade in paper_positions
        active_fade_id = await create_position(
            main_db,
            market_id="0xtest_active_fade",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="LOSS_FADE"
        )
        # Manually update status to OPEN
        await main_db.execute("UPDATE paper_positions SET order_status = 'OPEN' WHERE position_id = ?", (active_fade_id,))
        await main_db.commit()

        config = _mock_config(lossmomentum_skip_enabled=True)
        pos_mgr = MagicMock()
        pos_mgr.config = config
        pos_mgr._paper = True
        pos_mgr.open_position = AsyncMock(return_value=True)
        pos_mgr.open_profile_position = AsyncMock()

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
            db=main_db,
            skip_db=skip_db
        )

        success, reason = await engine.process_signal(signal_market, target_token_id="no_tok_sig", side="NO")
        assert success is False
        assert "active/unresolved" in reason
        pos_mgr.open_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_loss_fade_concurrency_blocks_new_fade_when_unresolved_skips_exist(self, main_db, skip_db):
        """Verify that DecisionEngine blocks new signal when unresolved skips exist in skip_db."""
        # 1. Insert markets
        trig_market = {
            "market_id": "0xtest_skip_trig",
            "question": "Will ETH resolve UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_trig",
            "no_token_id": "no_tok_trig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        active_skip_market = {
            "market_id": "0xtest_active_skip",
            "question": "Will ETH resolve UP? (Active)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_active",
            "no_token_id": "no_tok_active",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        signal_market = {
            "market_id": "0xtest_skip_sig",
            "question": "Will ETH resolve UP? (Signal)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_sig",
            "no_token_id": "no_tok_sig",
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 300,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(main_db, trig_market)
        await upsert_market(main_db, active_skip_market)
        await upsert_market(main_db, signal_market)

        # 2. Insert a closed LOSS position in main_db
        loss_pos_id = await create_position(
            main_db,
            market_id="0xtest_skip_trig",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        # 3. Insert an unresolved skipped trade in skip_db
        await skip_db.record_skip(
            market_id="0xtest_active_skip",
            question="Will ETH resolve UP?",
            side="NO",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20.0,
            trigger_position_id=loss_pos_id,
            symbol="ETH"
        )

        config = _mock_config(lossmomentum_skip_enabled=True)
        pos_mgr = MagicMock()
        pos_mgr.config = config
        pos_mgr._paper = True
        pos_mgr.open_position = AsyncMock(return_value=True)
        pos_mgr.open_profile_position = AsyncMock()

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
            db=main_db,
            skip_db=skip_db
        )

        success, reason = await engine.process_signal(signal_market, target_token_id="no_tok_sig", side="NO")
        assert success is False
        assert "active/unresolved" in reason
        pos_mgr.open_position.assert_not_called()


    @pytest.mark.asyncio
    async def test_concurrent_loss_fade_regime(self, main_db, skip_db):
        """Verify that multiple concurrent/overlapping signals are faded under the continuous recovery regime."""
        # 1. Insert markets
        trig_market = {
            "market_id": "0xregime_trig",
            "question": "ETH UP? (Trigger)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_trig",
            "no_token_id": "no_tok_trig",
            "closes_at": int(time.time()) - 1000,
        }
        sig1_market = {
            "market_id": "0xregime_sig1",
            "question": "ETH UP? (Signal 1)",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_token_id": "yes_tok_sig1",
            "no_token_id": "no_tok_sig1",
            "closes_at": int(time.time()) + 300,
        }
        sig2_market = {
            "market_id": "0xregime_sig2",
            "question": "ETH UP? (Signal 2)",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_token_id": "yes_tok_sig2",
            "no_token_id": "no_tok_sig2",
            "closes_at": int(time.time()) + 300,
        }
        sig3_market = {
            "market_id": "0xregime_sig3",
            "question": "ETH UP? (Signal 3)",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_token_id": "yes_tok_sig3",
            "no_token_id": "no_tok_sig3",
            "closes_at": int(time.time()) + 600,
        }
        await upsert_market(main_db, trig_market)
        await upsert_market(main_db, sig1_market)
        await upsert_market(main_db, sig2_market)
        await upsert_market(main_db, sig3_market)

        # 2. Insert a closed LOSS position in main_db
        loss_pos_id = await create_position(
            main_db,
            market_id="0xregime_trig",
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20,
            bot_version="v4.30",
            paper=True,
            notes="no_token_id",
            strategy_mode="TREND"
        )
        await close_position(main_db, loss_pos_id, exit_price=0.0, realised_pnl=-10.0, close_reason="RESOLUTION", paper=True)

        config = _mock_config(lossmomentum_skip_enabled=True)
        pos_mgr = MagicMock()
        pos_mgr.config = config
        pos_mgr._paper = True
        pos_mgr.open_position = AsyncMock(return_value=True)
        pos_mgr.open_profile_position = AsyncMock()

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
            db=main_db,
            skip_db=skip_db
        )

        # Signal 1 arrives: should engage LOSS_FADE
        success, reason = await engine.process_signal(sig1_market, target_token_id="no_tok_sig1", side="NO")
        assert success is True
        assert pos_mgr.open_position.call_count == 1
        assert pos_mgr.open_position.call_args[1]["strategy_mode"] == "LOSS_FADE"
        assert pos_mgr.open_position.call_args[1]["side"] == "YES"

        # Retrieve the skipped trend position corresponding to Signal 1 automatically recorded in skip_db
        unresolved_skips = await skip_db.get_unresolved_skips()
        assert len(unresolved_skips) == 1
        skip1_id = unresolved_skips[0]["position_id"]

        # Signal 2 arrives concurrently (Signal 1 is still unresolved): should be blocked under discrete FCFS!
        pos_mgr.open_position.reset_mock()
        success, reason = await engine.process_signal(sig2_market, target_token_id="no_tok_sig2", side="NO")
        assert success is False
        assert "active/unresolved" in reason
        pos_mgr.open_position.assert_not_called()

        # Resolve skipped trade for Signal 1 as a WIN (meaning trend side won, streak is broken)
        await skip_db.resolve_skip(skip1_id, exit_price=1.0, realised_pnl=10.0, closed_at=int(time.time()))

        # Signal 3 arrives: since the trend streak was broken by the skip1 WIN, Signal 3 should run in TREND mode!
        pos_mgr.open_position.reset_mock()
        success, reason = await engine.process_signal(sig3_market, target_token_id="no_tok_sig3", side="NO")
        assert success is True
        assert pos_mgr.open_position.call_count == 1
        assert pos_mgr.open_position.call_args[1]["strategy_mode"] == "TREND"
        assert pos_mgr.open_position.call_args[1]["side"] == "NO"


