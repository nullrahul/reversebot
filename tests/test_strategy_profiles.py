import os
import asyncio
import pytest
import pytest_asyncio
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

# Ensure we can import from src
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market, create_position, close_position
from src.db.queries import (
    create_profile_position,
    close_profile_position,
    get_open_profile_positions,
    get_profile_performance_summary,
    get_profile_history,
    get_profile_pnl_series
)
from src.strategy.resolver import MarketResolver
from src.strategy.profiles import ProfileManager


@pytest_asyncio.fixture
async def db():
    """Create a temporary SQLite database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _sample_market(market_id: str = "0xtest_profile", **overrides) -> dict:
    """Create a sample market dict for testing."""
    base = {
        "market_id": market_id,
        "question": "Will BTC go UP today?",
        "category": "Crypto",
        "yes_price": 0.55,
        "no_price": 0.45,
        "yes_volume_24h": 1500.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "closes_at": int(time.time()) + 86400,
        "is_flagged": 0,
        "flag_reason": None,
        "last_seen_above_threshold": None,
        "consecutive_cycles_above": 0,
        "yes_token_id": "yes_token_123",
        "no_token_id": "no_token_123",
        "slug": "btc-updown-5m-12345"
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Central Resolver Tests
# ---------------------------------------------------------------------------

class TestMarketResolver:

    @pytest.mark.asyncio
    async def test_resolver_caching_and_binance_fallback(self, db):
        """Test resolver retrieves and caches resolutions cleanly."""
        gamma_mock = AsyncMock()
        resolver = MarketResolver()

        # Let's mock a Binance kline fetch: outcome is UP (close > open)
        with patch.object(resolver, "_get_binance_resolution", AsyncMock(return_value="UP")) as mock_binance:
            # 1. Fetch outcome for YES contract
            res = await resolver.get_market_resolution(
                db, gamma_mock, "0xmarket", "btc", 5, 1700000000, "YES"
            )
            assert res is not None
            outcome, exit_price = res
            assert outcome == "UP"
            assert exit_price == 1.0  # YES wins on UP

            # Verify Binance method was called
            assert mock_binance.call_count == 1

            # 2. Call again with same params (cache hit should prevent API fetch)
            res2 = await resolver.get_market_resolution(
                db, gamma_mock, "0xmarket", "btc", 5, 1700000000, "YES"
            )
            assert res2 == res
            assert mock_binance.call_count == 1  # Still 1

            # 3. Call for opposing side (NO contract)
            res3 = await resolver.get_market_resolution(
                db, gamma_mock, "0xmarket", "btc", 5, 1700000000, "NO"
            )
            assert res3 is not None
            assert res3[0] == "UP"
            assert res3[1] == 0.0  # NO loses on UP
            assert mock_binance.call_count == 2  # 2 because YES and NO use different cache keys


# ---------------------------------------------------------------------------
# Profile Manager Tests
# ---------------------------------------------------------------------------

class TestProfileManager:

    @pytest.mark.asyncio
    async def test_profile_evaluation_triggers(self, db):
        """Test that ProfileManager accurately triggers profiles based on criteria."""
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock.config.reversal_spread_penalty = 0.01
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        market = _sample_market()
        await upsert_market(db, market)

        # 1. Insert a WIN trade so loss_skip is NOT allowed to trigger
        win_pid = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, win_pid, 1.0, 5.0, "RESOLUTION", paper=True)
        await db.execute("UPDATE paper_positions SET closed_at = 1000 WHERE position_id = ?", (win_pid,))
        await db.commit()

        # 1. Stop Loss Trigger (old Late Entry): triggers immediately under gates
        # Check no trigger when price is outside B3 gates (e.g. yes_price=0.65 > 0.60)
        market["yes_price"] = 0.65
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.65,
            size_usdc=10.0,
            shares=15.38,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=True,
            active_strategy="TREND"
        )
        # Verify stop_loss was NOT triggered
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "stop_loss" not in triggered_profiles
        assert "loss_skip" not in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # Insert a LOSS trade so loss_skip will trigger now!
        loss_pid = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, loss_pid, 0.0, -10.0, "RESOLUTION", paper=True)
        await db.execute("UPDATE paper_positions SET closed_at = 2000 WHERE position_id = ?", (loss_pid,))
        await db.commit()

        # Check triggers when price is inside gates (e.g. yes_price=0.60)
        market["yes_price"] = 0.60
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,
            size_usdc=10.0,
            shares=16.66,
            strategy_mode="TREND",
            elapsed_secs=45.0,
            persistence_score=0.50,
            should_block_for_loss=True,
            active_strategy="TREND"
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "stop_loss" in triggered_profiles

        # 2. Loss Skip Trigger: triggers because last_main_is_loss is True
        assert "loss_skip" in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # Insert a WIN trade again so loss_skip is NOT triggered
        win_pid2 = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, win_pid2, 1.0, 5.0, "RESOLUTION", paper=True)
        await db.execute("UPDATE paper_positions SET closed_at = 3000 WHERE position_id = ?", (win_pid2,))
        await db.commit()

        # Set should_block_for_loss = False (but even if True, it shouldn't trigger because database last trade is WIN)
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="TREND"
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "loss_skip" not in triggered_profiles

        # 4. Fade Reversal Trigger: Triggers continuously as a shadow strategy
        pos_mgr_mock.open_profile_position.reset_mock()

        # Verify it DOES trigger in TREND mode
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="TREND"
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "reversal" in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # Verify it DOES trigger in REVERSAL mode
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="REVERSAL"
        )
        # Check side was flipped to NO in virtual order
        triggered_calls = {call.kwargs.get("profile_name"): call.kwargs for call in pos_mgr_mock.open_profile_position.call_args_list}
        assert "reversal" in triggered_calls
        reversal_call = triggered_calls["reversal"]
        assert reversal_call.get("side") == "NO"
        assert reversal_call.get("target_token_id") == "no_token_123"
        # Trend price YES was 0.55. Trend price NO is 1.0 - 0.55 = 0.45.
        # Reversal price NO = 1.0 - 0.55 = 0.45
        assert abs(reversal_call.get("entry_price") - 0.45) < 0.001


    @pytest.mark.asyncio
    async def test_streak_depth_gate_blocks_profiles(self, db):
        """Verify that streak depth >= 6 blocks loss_skip and stop_loss (but not cheap_reversal)."""
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        market = _sample_market()
        await upsert_market(db, market)

        # Insert a LOSS trade so loss_skip would trigger if not blocked by depth
        loss_pid = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, loss_pid, 0.0, -10.0, "RESOLUTION", paper=True)

        # Call 1: High price (0.60) to test stop_loss and loss_skip depth block
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,
            size_usdc=10.0,
            shares=16.66,
            strategy_mode="TREND",
            elapsed_secs=45.0,
            persistence_score=0.70,
            should_block_for_loss=True,
            active_strategy="TREND",
            streak_depth=6
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "stop_loss" not in triggered_profiles
        assert "loss_skip" not in triggered_profiles
        assert "cheap_reversal" not in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # Call 2: Low price (0.30) to test cheap_reversal triggers (reversal_price = 0.70 >= 0.52)
        cheap_market = _sample_market(yes_price=0.30, no_price=0.70)
        await pm.evaluate_and_trigger(
            market=cheap_market,
            side="YES",
            entry_price=0.30,
            size_usdc=10.0,
            shares=33.33,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.70,
            should_block_for_loss=True,
            active_strategy="TREND",
            streak_depth=6,
            book_imbalance_at_entry=-0.40
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" in triggered_profiles

    @pytest.mark.asyncio
    async def test_cvd_exhaustion_blocks_profiles(self, db):
        """Verify that extreme CVD is soft-disabled and no longer blocks profile trades."""
        from datetime import datetime, timezone
        from unittest.mock import patch
        
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        await pm._init_opened_list()
        market = _sample_market()

        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)
        
        with patch("src.strategy.guardian.datetime", mock_dt):
            # YES side, CVD > 50k
            # CVD is soft-disabled, so standard profiles (YES) are NOT blocked by CVD > 50k.
            # late_entry is not triggered because trend price defaults to yes_price=0.55 (<0.58) in REVERSAL mode.
            await pm.evaluate_and_trigger(
                market=market,
                side="YES",
                entry_price=0.60,
                size_usdc=10.0,
                shares=16.66,
                strategy_mode="TREND",
                elapsed_secs=45.0,
                persistence_score=0.70,
                should_block_for_loss=False,
                active_strategy="REVERSAL",
                cvd_60s_at_entry=60000.0,
                streak_depth=2
            )
            triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "reversal" in triggered_profiles
            assert "streak_momentum" in triggered_profiles

            pos_mgr_mock.open_profile_position.reset_mock()
            pm.opened_stop_loss.clear()
            pm.opened_reversal_stop_loss.clear()
            pm.opened_probability_sl.clear()
            pm.opened_reversal_probability_sl.clear()
            pm.opened_stop_loss_filtered.clear()
            pm.opened_reversal_stop_loss_filtered.clear()

            # NO side, CVD < -50k
            # CVD is soft-disabled, so standard profiles (NO) are NOT blocked by CVD < -50k.
            await pm.evaluate_and_trigger(
                market=market,
                side="NO",
                entry_price=0.60,
                size_usdc=10.0,
                shares=16.66,
                strategy_mode="TREND",
                elapsed_secs=45.0,
                persistence_score=0.70,
                should_block_for_loss=False,
                active_strategy="REVERSAL",
                cvd_60s_at_entry=-60000.0,
                streak_depth=2,
                book_imbalance_at_entry=-0.40
            )
            triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "reversal" in triggered_profiles
            assert "streak_momentum" in triggered_profiles
            assert "cheap_reversal" not in triggered_profiles
            assert "cheap_reversal_v2" not in triggered_profiles

            pos_mgr_mock.open_profile_position.reset_mock()
            pm.opened_stop_loss.clear()
            pm.opened_reversal_stop_loss.clear()
            pm.opened_probability_sl.clear()
            pm.opened_reversal_probability_sl.clear()
            pm.opened_stop_loss_filtered.clear()
            pm.opened_reversal_stop_loss_filtered.clear()

            # Cheap reversal profile is also not blocked by CVD.
            # Test cheap_reversal profile under YES side, CVD > 50k.
            cheap_market = _sample_market(yes_price=0.30, no_price=0.70)
            await pm.evaluate_and_trigger(
                market=cheap_market,
                side="YES",
                entry_price=0.35,
                size_usdc=10.0,
                shares=33.33,
                strategy_mode="TREND",
                elapsed_secs=45.0,
                persistence_score=0.70,
                should_block_for_loss=False,
                active_strategy="REVERSAL",
                cvd_60s_at_entry=60000.0,
                streak_depth=2,
                book_imbalance_at_entry=-0.40
            )
            triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "cheap_reversal" in triggered_profiles
            assert "cheap_reversal_v2" in triggered_profiles

            pos_mgr_mock.open_profile_position.reset_mock()
            pm.opened_stop_loss.clear()
            pm.opened_reversal_stop_loss.clear()
            pm.opened_probability_sl.clear()
            pm.opened_reversal_probability_sl.clear()
            pm.opened_stop_loss_filtered.clear()
            pm.opened_reversal_stop_loss_filtered.clear()
            
            # Test cheap_reversal price gate logic (reversal_price >= 0.52)
            # 1. Reversal price = 0.55 (triggers)
            gate_market_ok = _sample_market(yes_price=0.55, no_price=0.45)
            await pm.evaluate_and_trigger(
                market=gate_market_ok,
                side="NO",  # reversal side is YES (0.55)
                entry_price=0.45,
                size_usdc=10.0,
                shares=22.2,
                strategy_mode="TREND",
                elapsed_secs=45.0,
                persistence_score=0.70,
                should_block_for_loss=False,
                active_strategy="TREND",
                streak_depth=2,
                book_imbalance_at_entry=-0.40
            )
            triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "cheap_reversal" in triggered_profiles

            pos_mgr_mock.open_profile_position.reset_mock()

            # 2. Reversal price = 0.45 (blocked)
            await pm.evaluate_and_trigger(
                market=gate_market_ok,
                side="YES",  # reversal side is NO (0.45)
                entry_price=0.55,
                size_usdc=10.0,
                shares=18.18,
                strategy_mode="TREND",
                elapsed_secs=45.0,
                persistence_score=0.70,
                should_block_for_loss=False,
                active_strategy="TREND",
                streak_depth=2
            )
            triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "cheap_reversal" not in triggered_profiles



    @pytest.mark.asyncio
    async def test_independent_profile_pricing_under_reversal_mode(self, db):
        """
        Verify that virtual profiles compute their prices and shares independently
        of whether the main bot executes in TREND or REVERSAL mode.
        """
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock.config.reversal_spread_penalty = 0.01
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        
        # Trend side is YES. yes_price=0.60, no_price=0.40
        market = _sample_market(yes_price=0.60, no_price=0.40)
        await upsert_market(db, market)

        # Insert a LOSS trade so loss_skip will trigger
        loss_pid = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, loss_pid, 0.0, -10.0, "RESOLUTION", paper=True)

        # Main bot is in REVERSAL mode, so main bot's entry_price is NO price = 0.40.
        # But standard profiles (like loss_skip) should still buy YES at 0.60,
        # and reversal profile should fade to NO at 1.0 - 0.60 + 0.01 = 0.41.
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,  # Flipped/Reversal price passed by main bot
            size_usdc=10.0,
            shares=25.0,       # Flipped/Reversal shares passed by main bot
            strategy_mode="REVERSAL",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=True,
            active_strategy="REVERSAL"
        )
        
        # Get calls to open_profile_position
        calls = {call.kwargs.get("profile_name"): call.kwargs for call in pos_mgr_mock.open_profile_position.call_args_list}
        
        # Verify loss_skip profile (which trades on reversal side):
        # Entry price should be reversal price (0.40) and shares should be 10.0 / 0.40 = 25.0
        assert "loss_skip" in calls
        loss_skip_call = calls["loss_skip"]
        assert loss_skip_call["side"] == "NO"
        assert abs(loss_skip_call["entry_price"] - 0.40) < 0.001
        assert abs(loss_skip_call["shares"] - 25.00) < 0.01
        assert loss_skip_call["strategy_mode"] == "LOSS_FADE"

        # Verify reversal profile (counter-trend):
        # Entry price should be reversal price (0.40) and shares should be 10.0 / 0.40 = 25.00
        assert "reversal" in calls
        reversal_call = calls["reversal"]
        assert reversal_call["side"] == "NO"
        assert abs(reversal_call["entry_price"] - 0.40) < 0.001
        assert abs(reversal_call["shares"] - 25.00) < 0.01
        assert reversal_call["strategy_mode"] == "REVERSAL"

    @pytest.mark.asyncio
    async def test_reversal_stop_loss_blocks_on_cheap_price(self, db):
        """
        Verify that reversal_stop_loss blocks orders when reversal_price is < 0.30 or > 0.70,
        preventing sub-dollar penny trades (e.g. 0.01) even if market metadata has stale 0.45 midpoint.
        """
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        # Market has stale 0.495 midpoint in dict, but live entry_price is 0.99
        market = _sample_market(yes_price=0.495, no_price=0.505)
        await upsert_market(db, market)

        # 1. DOWN streak where live NO price is 0.99 -> reversal YES price is 0.01
        await pm.evaluate_and_trigger(
            market=market,
            side="NO",
            entry_price=0.99,
            size_usdc=10.0,
            shares=10.1,
            strategy_mode="TREND",
            elapsed_secs=45.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2
        )
        calls = [c.kwargs.get("profile_name") for c in pos_mgr_mock.open_profile_position.call_args_list]
        assert "reversal_stop_loss" not in calls
        assert "reversal_stop_loss_filtered" not in calls

        pos_mgr_mock.open_profile_position.reset_mock()
        pm.opened_reversal_stop_loss.clear()
        pm.opened_reversal_stop_loss_filtered.clear()

        # 2. Normal market where live NO price is 0.55 -> reversal YES price is 0.45 (allowed)
        await pm.evaluate_and_trigger(
            market=market,
            side="NO",
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=45.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2
        )
        calls_ok = [c.kwargs.get("profile_name") for c in pos_mgr_mock.open_profile_position.call_args_list]
        assert "reversal_stop_loss" in calls_ok

    @pytest.mark.asyncio
    async def test_stop_loss_profile_monitoring_and_exit(self, db):
        """Test that monitor_positions detects stop_loss profile positions and stops them out on a 12% drop."""
        import json
        from src.strategy.position_mgr import PositionManager
        pm_instance = PositionManager(config=MagicMock(), clob_client=MagicMock(), db=db, ws_client=MagicMock())
        
        market = _sample_market(market_id="0xstop_loss_market")
        await upsert_market(db, market)
        
        notes_payload = json.dumps({"tid": "token_sl_123", "strategy": "TREND", "streak_depth": 2})
        pid = await create_profile_position(
            db=db,
            market_id=market["market_id"],
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20.0,
            bot_version="v4.50",
            notes=notes_payload,
            side="YES",
            profile_name="stop_loss"
        )
        
        pm_instance.ws.get_best_bid.return_value = 0.45
        pm_instance.ws.get_best_ask.return_value = 0.46
        pm_instance.ws.get_live_price.return_value = 0.45
        
        await pm_instance.monitor_positions()
        
        open_positions = await get_open_profile_positions(db)
        assert len(open_positions) == 1
        assert open_positions[0]["order_status"] == "OPEN"
        
        pm_instance.ws.get_best_bid.return_value = 0.38
        pm_instance.ws.get_best_ask.return_value = 0.38
        
        await pm_instance.monitor_positions()
        
        cursor = await db.execute("SELECT * FROM profile_positions WHERE position_id = ?", (pid,))
        closed_pos = dict(await cursor.fetchone())
        assert closed_pos["order_status"] == "FILLED"
        assert closed_pos["close_reason"] == "STOP_LOSS"
        assert closed_pos["exit_price"] == 0.38
        assert closed_pos["realised_pnl"] == (0.38 * 20.0 - 10.0)

    @pytest.mark.asyncio
    async def test_reversal_stop_loss_profile_monitoring_and_exit(self, db):
        """Test that monitor_positions detects reversal_stop_loss profile positions and stops them out on a 20% drop."""
        import json
        from src.strategy.position_mgr import PositionManager
        pm_instance = PositionManager(config=MagicMock(), clob_client=MagicMock(), db=db, ws_client=MagicMock())
        
        market = _sample_market(market_id="0xreversal_sl_market")
        await upsert_market(db, market)
        
        notes_payload = json.dumps({"tid": "token_sl_456", "strategy": "REVERSAL", "streak_depth": 2})
        pid = await create_profile_position(
            db=db,
            market_id=market["market_id"],
            entry_price=0.50,
            size_usdc=10.0,
            shares_bought=20.0,
            bot_version="v4.50",
            notes=notes_payload,
            side="YES",
            profile_name="reversal_stop_loss"
        )
        
        pm_instance.ws.get_best_bid.return_value = 0.45
        pm_instance.ws.get_best_ask.return_value = 0.46
        pm_instance.ws.get_live_price.return_value = 0.45
        
        await pm_instance.monitor_positions()
        
        open_positions = await get_open_profile_positions(db)
        # Filters by profile_name inside get_open_profile_positions if profile is specified, or returns all
        # Let's verify if there is any other open position
        open_rs_positions = [pos for pos in open_positions if pos["profile_name"] == "reversal_stop_loss"]
        assert len(open_rs_positions) == 1
        assert open_rs_positions[0]["order_status"] == "OPEN"
        
        pm_instance.ws.get_best_bid.return_value = 0.38
        pm_instance.ws.get_best_ask.return_value = 0.38
        
        await pm_instance.monitor_positions()
        
        cursor = await db.execute("SELECT * FROM profile_positions WHERE position_id = ?", (pid,))
        closed_pos = dict(await cursor.fetchone())
        assert closed_pos["order_status"] == "FILLED"
        assert closed_pos["close_reason"] == "STOP_LOSS"
        assert closed_pos["exit_price"] == 0.38
        assert closed_pos["realised_pnl"] == (0.38 * 20.0 - 10.0)


# ---------------------------------------------------------------------------
# Database Queries Tests
# ---------------------------------------------------------------------------

class TestProfileDatabaseQueries:

    @pytest.mark.asyncio
    async def test_crud_and_telemetry_summaries(self, db):
        """Test database helper functions for profile positions."""
        market = _sample_market()
        await upsert_market(db, market)

        # 1. Create a profile position
        pid = await create_profile_position(
            db=db,
            market_id=market["market_id"],
            entry_price=0.55,
            size_usdc=10.0,
            shares_bought=18.18,
            bot_version="v4.50",
            notes="{}",
            side="YES",
            win_prob_estimate=0.55,
            kelly_fraction_used=0.05,
            bid_ask_spread_at_entry=0.01,
            book_imbalance_at_entry=-0.05,
            cvd_60s_at_entry=1500.0,
            correlated_position_open=0,
            seconds_to_close_at_entry=180.0,
            market_age_at_entry_pct=0.45,
            bankroll_at_entry=200.0,
            persistence_score=0.56,
            strategy_mode="TREND",
            profile_name="cheap_reversal"
        )
        assert pid.startswith("virtual_cheap_reversal_")

        # Check open positions
        open_positions = await get_open_profile_positions(db)
        assert len(open_positions) == 1
        pos = open_positions[0]
        assert pos["position_id"] == pid
        assert pos["profile_name"] == "cheap_reversal"
        assert pos["order_status"] == "OPEN"
        assert pos["persistence_score"] == 0.56

        # 2. Close the profile position
        await close_profile_position(
            db=db,
            position_id=pid,
            exit_price=1.0,
            realised_pnl=8.18,
            close_reason="RESOLUTION"
        )

        # Confirm open is now empty
        open_positions = await get_open_profile_positions(db)
        assert len(open_positions) == 0

        # Check history
        history = await get_profile_history(db, "cheap_reversal")
        assert len(history) == 1
        assert history[0]["position_id"] == pid
        assert history[0]["realised_pnl"] == 8.18
        assert history[0]["close_reason"] == "RESOLUTION"

        # Check performance summary
        summary = await get_profile_performance_summary(db, "cheap_reversal")
        assert summary["total_closed"] == 1
        assert summary["wins"] == 1
        assert summary["losses"] == 0
        assert summary["win_rate"] == 100.0
        assert summary["total_realised_pnl"] == 8.18

        # Check PNL series
        series = await get_profile_pnl_series(db, "cheap_reversal")
        assert len(series) == 1
        assert series[0]["pnl"] == 8.18


class TestLossSkipSpecialRules:

    @pytest.mark.asyncio
    async def test_loss_skip_concurrency_allowed(self, db):
        """Verify that concurrent loss_skip positions are allowed globally (FCFS removed)."""
        from src.strategy.position_mgr import PositionManager
        
        market_a = _sample_market("market_a")
        market_b = _sample_market("market_b")
        await upsert_market(db, market_a)
        await upsert_market(db, market_b)

        config = MagicMock()
        config.paper_trade = True
        config.bot_version = "v4.81"
        clob = AsyncMock()
        mgr = PositionManager(config, clob, db)

        # 1. Open the first position for loss_skip — should succeed
        success_a = await mgr.open_profile_position(
            profile_name="loss_skip",
            market_id="market_a",
            entry_price=0.50,
            size_usdc=10.0,
            shares=20.0,
            side="YES"
        )
        assert success_a is True

        # 2. Attempt to open a second concurrent position for loss_skip — should also succeed now
        success_b = await mgr.open_profile_position(
            profile_name="loss_skip",
            market_id="market_b",
            entry_price=0.50,
            size_usdc=10.0,
            shares=20.0,
            side="YES"
        )
        assert success_b is True

        # Verify that both are currently open in the database
        open_positions = await get_open_profile_positions(db)
        ls_positions = [p for p in open_positions if p["profile_name"] == "loss_skip"]
        assert len(ls_positions) == 2
        assert {p["market_id"] for p in ls_positions} == {"market_a", "market_b"}

    @pytest.mark.asyncio
    async def test_loss_skip_flipped_resolution_logic(self, db):
        """Verify resolution logic for loss_skip (which trades on reversal side and resolves naturally)."""
        from src.strategy.position_mgr import PositionManager
        
        market_win = _sample_market("market_win")
        market_loss = _sample_market("market_loss")
        await upsert_market(db, market_win)
        await upsert_market(db, market_loss)

        config = MagicMock()
        config.paper_trade = True
        config.bot_version = "v4.81"
        clob = AsyncMock()
        mgr = PositionManager(config, clob, db)

        # 1. Open virtual position for skipped trade on market_win (which will resolve to a loss in reality, meaning a win for skip)
        await mgr.open_profile_position(
            profile_name="loss_skip",
            market_id="market_win",
            entry_price=0.50,
            size_usdc=10.0,
            shares=20.0,
            side="NO"
        )
        
        # 2. Open virtual position for skipped trade on market_loss (which will resolve to a win in reality, meaning a loss for skip)
        # Note: We must close the first one first to satisfy FCFS!
        # First, let's mock resolver resolution for market_win to return (YES, 0.0) -> actual trade lost
        mgr.gamma = AsyncMock()
        mgr.gamma._get = AsyncMock(return_value={
            "outcomePrices": '["0.00", "1.00"]',
            "outcomes": '["YES", "NO"]',
        })
        # Set market_win closes_at to in the past so check_resolutions evaluates it
        await db.execute("UPDATE markets SET closes_at = 1 WHERE market_id = 'market_win'")
        await db.commit()
        
        await mgr.check_resolutions()
        
        # Verify it closed as a win (exit_price=1.0, realised_pnl=10.0)
        history_win = await get_profile_history(db, "loss_skip")
        assert len(history_win) == 1
        assert history_win[0]["market_id"] == "market_win"
        assert history_win[0]["exit_price"] == 1.0
        assert history_win[0]["realised_pnl"] == 10.0  # 20 shares * 1.0 - 10.0 cost

        # 3. Now that market_win is closed, we can open market_loss position under FCFS
        await mgr.open_profile_position(
            profile_name="loss_skip",
            market_id="market_loss",
            entry_price=0.50,
            size_usdc=10.0,
            shares=20.0,
            side="NO"
        )
        
        # Mock resolver resolution for market_loss to return (YES, 1.0) -> actual trade won
        mgr.gamma._get = AsyncMock(return_value={
            "outcomePrices": '["1.00", "0.00"]',
            "outcomes": '["YES", "NO"]',
        })
        await db.execute("UPDATE markets SET closes_at = 1 WHERE market_id = 'market_loss'")
        await db.commit()
        
        await mgr.check_resolutions()
        
        # Verify it closed as a loss (exit_price=0.0, realised_pnl=-10.0)
        history_loss = await get_profile_history(db, "loss_skip")
        assert len(history_loss) == 2
        # Sort by closed_at or position_id
        resolved_loss_pos = [p for p in history_loss if p["market_id"] == "market_loss"][0]
        assert resolved_loss_pos["exit_price"] == 0.0
        assert resolved_loss_pos["realised_pnl"] == -10.0

    @pytest.mark.asyncio
    async def test_loss_skip_blocks_on_reversal_price_gate(self, db):
        """Verify that loss_skip virtual position is blocked when the reversal price is >= 0.50."""
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)

        # 1. Market A where reversal price >= 0.50 (opposing is NO, price is 0.50)
        market_a = _sample_market("market_a", yes_price=0.50, no_price=0.50)
        await upsert_market(db, market_a)

        # Insert a LOSS trade so loss_skip would trigger if not blocked by price
        loss_pid = await create_position(db, "market_a", 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, loss_pid, 0.0, -10.0, "RESOLUTION", paper=True)

        await pm.evaluate_and_trigger(
            market=market_a,
            side="YES",
            entry_price=0.50,
            size_usdc=10.0,
            shares=20.0,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=True,
            active_strategy="TREND",
            streak_depth=3
        )
        # Should not have triggered loss_skip due to price >= 0.50
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "loss_skip" not in triggered_profiles

        # 2. Market B where reversal price < 0.50 (opposing is NO, price is 0.45)
        pos_mgr_mock.open_profile_position.reset_mock()
        market_b = _sample_market("market_b", yes_price=0.55, no_price=0.45)
        await upsert_market(db, market_b)

        await pm.evaluate_and_trigger(
            market=market_b,
            side="YES",
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=True,
            active_strategy="TREND",
            streak_depth=3
        )
        # Should have triggered loss_skip due to price < 0.50
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "loss_skip" in triggered_profiles


class TestStreakMomentumProfile:

    @pytest.mark.asyncio
    async def test_streak_momentum_profile_trigger(self, db):
        """Verify that streak_momentum triggers virtual trades with TREND strategy mode."""
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        market = _sample_market()

        # 1. Under normal streak depth (< 6), it should trigger streak_momentum
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,
            size_usdc=10.0,
            shares=16.66,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=3
        )
        
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "streak_momentum" in triggered_profiles
        
        # Verify trend side was preserved (YES) and strategy_mode was TREND
        streak_momentum_call = [call.kwargs for call in pos_mgr_mock.open_profile_position.call_args_list if call.kwargs.get("profile_name") == "streak_momentum"][0]
        assert streak_momentum_call["side"] == "YES"
        assert streak_momentum_call["strategy_mode"] == "TREND"
        assert abs(streak_momentum_call["entry_price"] - 0.60) < 0.001

        # 2. Under high streak depth (>= 6), it should be blocked/skipped
        pos_mgr_mock.open_profile_position.reset_mock()
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,
            size_usdc=10.0,
            shares=16.66,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=6
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "streak_momentum" not in triggered_profiles

        # 3. Under loss skip condition (last_main_is_loss is True), streak_momentum should NOT be blocked/skipped anymore
        # Insert a LOSS trade in the database so last_main_is_loss evaluates to True
        await upsert_market(db, market)
        loss_pid = await create_position(db, market["market_id"], 0.5, 10.0, 20.0, "v4.80", paper=True)
        await close_position(db, loss_pid, 0.0, -10.0, "RESOLUTION", paper=True)
        await db.execute("UPDATE paper_positions SET closed_at = 2000 WHERE position_id = ?", (loss_pid,))
        await db.commit()

        pos_mgr_mock.open_profile_position.reset_mock()
        await pm.evaluate_and_trigger(
            market=market,
            side="YES",
            entry_price=0.60,
            size_usdc=10.0,
            shares=16.66,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.50,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=3
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "streak_momentum" in triggered_profiles


class TestCheapReversalSpecialRules:
    @pytest.mark.asyncio
    async def test_cheap_reversal_profile_trigger(self, db):
        """Verify that cheap_reversal triggers when 0.52 <= reversal_price < 0.80 and imbalance < -0.15, and blocks otherwise."""
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.config = MagicMock()
        pos_mgr_mock._paper = True
        pos_mgr_mock.open_profile_position = AsyncMock()

        pm = ProfileManager(db, pos_mgr_mock)
        
        # 1. Reversal price = 0.55 >= 0.52, imbalance = -0.20 (Should trigger)
        market_trigger = _sample_market(yes_price=0.55, no_price=0.45)
        await pm.evaluate_and_trigger(
            market=market_trigger,
            side="NO",  # trend side is NO, reversal side is YES (0.55)
            entry_price=0.45,
            size_usdc=10.0,
            shares=22.22,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2,
            book_imbalance_at_entry=-0.40
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # 2. Reversal price = 0.45 < 0.52 (Should block)
        await pm.evaluate_and_trigger(
            market=market_trigger,
            side="YES",  # trend side is YES, reversal side is NO (0.45)
            entry_price=0.55,
            size_usdc=10.0,
            shares=18.18,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2,
            book_imbalance_at_entry=-0.20
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" not in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # 3. Reversal price = 0.80 >= 0.80 (Should block)
        market_expensive = _sample_market(yes_price=0.20, no_price=0.80)
        await pm.evaluate_and_trigger(
            market=market_expensive,
            side="YES",  # trend side is YES, reversal side is NO (0.80)
            entry_price=0.20,
            size_usdc=10.0,
            shares=12.5,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2,
            book_imbalance_at_entry=-0.20
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" not in triggered_profiles

        pos_mgr_mock.open_profile_position.reset_mock()

        # 4. Imbalance = 0.0 >= -0.15 (Should block)
        await pm.evaluate_and_trigger(
            market=market_trigger,
            side="NO",  # trend side is NO, reversal side is YES (0.55)
            entry_price=0.45,
            size_usdc=10.0,
            shares=22.22,
            strategy_mode="TREND",
            elapsed_secs=15.0,
            persistence_score=0.70,
            should_block_for_loss=False,
            active_strategy="TREND",
            streak_depth=2,
            book_imbalance_at_entry=0.0
        )
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" not in triggered_profiles

    @pytest.mark.asyncio
    async def test_cheap_profile_blocked_on_15m_timeframe(self, db):
        """Verify that cheap_reversal profile does not trigger on 15-minute markets."""
        from src.strategy.decision import DecisionEngine
        from src.strategy.validator import SignalValidator
        
        mock_config = MagicMock()
        mock_config.timeframe_15m_enabled = False # disable 15m for main
        mock_config.paper_trade = True
        mock_config.drawdown_halted = False
        mock_config.concurrency_limit = 3
        mock_config.slippage_retry_limit = 1
        
        validator = MagicMock()
        clob = MagicMock()
        scanner = MagicMock()
        scanner.config = mock_config
        scanner._get_market_key_info = MagicMock(return_value=("BTC", 15))
        
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.20)
        pos_mgr_mock.ws.get_best_bid = MagicMock(return_value=0.19)
        pos_mgr_mock.open_profile_position = AsyncMock()
        
        decision = DecisionEngine(mock_config, validator, pos_mgr_mock, clob, db, scanner=scanner)
        decision.soft_audit_db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        decision.rejection_logger = MagicMock()
        decision.rejection_logger.log_rejection = AsyncMock()
        
        market = {
            "market_id": "test_m_15m_disabled",
            "question": "Will BTC end UP or DOWN - 15 Minutes?",
            "yes_price": 0.20,
            "no_price": 0.80,
            "window_minutes": 15,
            "closes_at": int(time.time()) + 600,
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
        }
        
        success, reason = await decision.process_signal(market, side="YES")
        
        # 1. Main bot must reject the signal due to 15m disabled
        assert success is False
        assert "15-minute timeframe is disabled" in reason
        
        # 2. Virtual cheap_reversal profile MUST have been blocked due to 15m timeframe constraint
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" not in triggered_profiles
        
        # Cleanup
        if os.path.exists(decision.soft_audit_db_path):
            try:
                os.unlink(decision.soft_audit_db_path)
            except:
                pass

    @pytest.mark.asyncio
    async def test_cheap_profile_runs_when_drawdown_halted_for_main(self, db):
        """Verify that cheap profile evaluates/triggers even when main bot is halted due to max drawdown."""
        from src.strategy.decision import DecisionEngine
        from config.config import BotConfig
        
        mock_config = MagicMock(spec=BotConfig)
        mock_config.bot_version = "v5.13-test"
        mock_config.paper_trade = True
        mock_config.position_size_usdc = 10.0
        mock_config.timeframe_15m_enabled = True
        mock_config.min_entry_price = 0.30
        mock_config.max_entry_price = 0.60
        mock_config.lossmomentum_skip_enabled = False
        mock_config.global_cooldown = 0.0

        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)
        scanner = MagicMock()
        scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
        scanner.last_traded_close_ts = {}
        
        validator = MagicMock()
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.is_connected = True
        pos_mgr_mock.ws.bid_cache = {}
        pos_mgr_mock.ws.ask_cache = {}
        pos_mgr_mock.ws.price_cache = {}
        pos_mgr_mock.ws.book_cache = {}
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.25)
        pos_mgr_mock.ws.get_best_bid = MagicMock(return_value=0.24)
        pos_mgr_mock.ws.get_full_book = MagicMock(return_value={
            "bids": [{"price": "0.24", "size": "100"}],
            "asks": [{"price": "0.25", "size": "900"}]
        })
        pos_mgr_mock.open_profile_position = AsyncMock()
        
        decision = DecisionEngine(mock_config, validator, pos_mgr_mock, clob, db, scanner=scanner)
        decision.soft_audit_db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        decision.rejection_logger = MagicMock()
        decision.rejection_logger.log_rejection = AsyncMock()
        
        # Force drawdown halted state
        decision.drawdown_halted = True
        
        market = {
            "market_id": "test_m_drawdown",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "yes_price": 0.25,
            "no_price": 0.75,
            "window_minutes": 5,
            "closes_at": int(time.time()) + 200,
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
        }
        
        success, reason = await decision.process_signal(market, side="YES")
        
        # 1. Main bot must reject the signal due to drawdown
        assert success is False
        assert "Max Drawdown Breaker active" in reason
        
        # 2. Virtual cheap_reversal profile MUST have been triggered
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" in triggered_profiles
        
        # Cleanup
        if os.path.exists(decision.soft_audit_db_path):
            try:
                os.unlink(decision.soft_audit_db_path)
            except:
                pass

    @pytest.mark.asyncio
    async def test_cheap_profile_runs_when_price_out_of_bounds_for_main(self, db):
        """Verify that cheap profile triggers for cheap entries, but main bot rejects them if below min_entry_price."""
        from src.strategy.decision import DecisionEngine
        from config.config import BotConfig
        
        mock_config = MagicMock(spec=BotConfig)
        mock_config.bot_version = "v5.13-test"
        mock_config.paper_trade = True
        mock_config.position_size_usdc = 10.0
        mock_config.timeframe_15m_enabled = True
        mock_config.min_entry_price = 0.30  # Min price is 0.30
        mock_config.max_entry_price = 0.60
        mock_config.lossmomentum_skip_enabled = False
        mock_config.global_cooldown = 0.0

        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)
        scanner = MagicMock()
        scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
        scanner.last_traded_close_ts = {}
        
        validator = MagicMock()
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.is_connected = True
        pos_mgr_mock.ws.bid_cache = {}
        pos_mgr_mock.ws.ask_cache = {}
        pos_mgr_mock.ws.price_cache = {}
        pos_mgr_mock.ws.book_cache = {}
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.25)  # Price is 0.25 (below 0.30)
        pos_mgr_mock.ws.get_best_bid = MagicMock(return_value=0.24)
        pos_mgr_mock.ws.get_full_book = MagicMock(return_value={
            "bids": [{"price": "0.24", "size": "100"}],
            "asks": [{"price": "0.25", "size": "900"}]
        })
        pos_mgr_mock.open_profile_position = AsyncMock()
        
        decision = DecisionEngine(mock_config, validator, pos_mgr_mock, clob, db, scanner=scanner)
        decision.soft_audit_db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        decision.rejection_logger = MagicMock()
        decision.rejection_logger.log_rejection = AsyncMock()
        
        market = {
            "market_id": "test_m_cheap_price",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "yes_price": 0.25,
            "no_price": 0.75,
            "window_minutes": 5,
            "closes_at": int(time.time()) + 200,
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
        }
        
        success, reason = await decision.process_signal(market, side="YES")
        
        # 1. Main bot must reject because 0.25 < 0.30
        assert success is False
        assert "below floor" in reason and "0.3" in reason
        
        # 2. Virtual cheap_reversal profile MUST have been triggered
        triggered_profiles = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
        assert "cheap_reversal" in triggered_profiles
        
        # Cleanup
        if os.path.exists(decision.soft_audit_db_path):
            try:
                os.unlink(decision.soft_audit_db_path)
            except:
                pass


class TestGuardianABTesting:

    @pytest.mark.asyncio
    async def test_evaluate_time_regime(self):
        from src.strategy.guardian import Guardian
        guardian = Guardian(MagicMock(), MagicMock())
        
        # Test reversal_stop_loss_filtered between 08:00 and 12:00
        # When UTC hour is 9
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 18, 9, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.evaluate_time_regime("reversal_stop_loss_filtered") is False
            assert guardian.evaluate_time_regime("stop_loss_filtered") is True
            
        # When UTC hour is 13
        mock_dt.now.return_value = datetime(2026, 6, 18, 13, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.evaluate_time_regime("reversal_stop_loss_filtered") is True
            
        # Test stop_loss_filtered between 20:00 and 02:00
        # When UTC hour is 21
        mock_dt.now.return_value = datetime(2026, 6, 18, 21, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.evaluate_time_regime("stop_loss_filtered") is False
            assert guardian.evaluate_time_regime("reversal_stop_loss_filtered") is True
            
        # When UTC hour is 1
        mock_dt.now.return_value = datetime(2026, 6, 19, 1, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.evaluate_time_regime("stop_loss_filtered") is False
            
        # When UTC hour is 15
        mock_dt.now.return_value = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.evaluate_time_regime("stop_loss_filtered") is True

    @pytest.mark.asyncio
    async def test_weekend_modifier(self):
        from src.strategy.guardian import Guardian
        guardian = Guardian(MagicMock(), MagicMock())
        
        # Test weekday (Thursday: weekday() = 3)
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.get_scaled_size("reversal_stop_loss_filtered", 10.0) == 10.0
            assert guardian.get_scaled_size("stop_loss_filtered", 10.0) == 10.0
            
        # Test Friday (weekday() = 4)
        mock_dt.now.return_value = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.get_scaled_size("reversal_stop_loss_filtered", 10.0) == 12.5
            assert guardian.get_scaled_size("stop_loss_filtered", 10.0) == 7.5

        # Test Sunday (weekday() = 6)
        mock_dt.now.return_value = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert guardian.get_scaled_size("reversal_stop_loss_filtered", 10.0) == 12.5
            assert guardian.get_scaled_size("stop_loss_filtered", 10.0) == 7.5

    @pytest.mark.asyncio
    async def test_ab_shadow_profiles_trigger(self, db):
        from datetime import datetime, timezone
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.open_profile_position = AsyncMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.5)
        
        pm = ProfileManager(db, pos_mgr_mock)
        await pm._init_opened_list()
        
        market = _sample_market(
            market_id="ab_test_market",
            yes_price=0.5,
            no_price=0.5,
            yes_token_id="yes_token_123",
            no_token_id="no_token_123",
            closes_at=int(time.time()) + 1000
        )
        
        # Scenario 1: Allowed by Guardian (UTC Thursday 15:00)
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)
        
        with patch("src.strategy.guardian.datetime", mock_dt):
            await pm.evaluate_and_trigger(
                market=market,
                side="YES",
                entry_price=0.5,
                size_usdc=10.0,
                shares=20.0,
                strategy_mode="TREND",
                elapsed_secs=10.0,
                persistence_score=0.6,
                should_block_for_loss=False,
                active_strategy="TREND"
            )
            
            # Verify stop_loss and stop_loss_filtered are both triggered
            calls = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "stop_loss" in calls
            assert "stop_loss_filtered" in calls
            
            # Verify size_usdc is not scaled on Thursday
            filtered_call = [call for call in pos_mgr_mock.open_profile_position.call_args_list if call.kwargs.get("profile_name") == "stop_loss_filtered"][0]
            assert filtered_call.kwargs.get("size_usdc") == 10.0
            assert filtered_call.kwargs.get("shares") == 20.0

    @pytest.mark.asyncio
    async def test_ab_shadow_profiles_weekend_scaling_and_floor(self, db):
        from datetime import datetime, timezone
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.open_profile_position = AsyncMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.5)
        
        pm = ProfileManager(db, pos_mgr_mock)
        await pm._init_opened_list()
        
        market = _sample_market(
            market_id="ab_test_market_weekend",
            yes_price=0.5,
            no_price=0.5,
            yes_token_id="yes_token_123",
            no_token_id="no_token_123",
            closes_at=int(time.time()) + 1000
        )
        
        # Scenario 2: Weekend scaling (Friday 15:00 UTC) -> stop_loss_filtered scaled down to 7.5 USDC size.
        # Trend price is 0.5, so 7.5 / 0.5 = 15 shares (>= 6.0 shares, so it triggers).
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 19, 15, 0, 0, tzinfo=timezone.utc)
        
        with patch("src.strategy.guardian.datetime", mock_dt):
            await pm.evaluate_and_trigger(
                market=market,
                side="YES",
                entry_price=0.5,
                size_usdc=10.0,
                shares=20.0,
                strategy_mode="TREND",
                elapsed_secs=10.0,
                persistence_score=0.6,
                should_block_for_loss=False,
                active_strategy="TREND"
            )
            
            calls = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "stop_loss_filtered" in calls
            filtered_call = [call for call in pos_mgr_mock.open_profile_position.call_args_list if call.kwargs.get("profile_name") == "stop_loss_filtered"][0]
            assert filtered_call.kwargs.get("size_usdc") == 7.5
            assert filtered_call.kwargs.get("shares") == 15.0

    @pytest.mark.asyncio
    async def test_ab_shadow_profiles_graceful_floor_rejection(self, db):
        from datetime import datetime, timezone
        pos_mgr_mock = MagicMock()
        pos_mgr_mock.open_profile_position = AsyncMock()
        pos_mgr_mock.ws = MagicMock()
        pos_mgr_mock.ws.get_best_ask = MagicMock(return_value=0.5)
        
        pm = ProfileManager(db, pos_mgr_mock)
        await pm._init_opened_list()
        
        market = _sample_market(
            market_id="ab_test_market_floor",
            yes_price=0.5,
            no_price=0.5,
            yes_token_id="yes_token_123",
            no_token_id="no_token_123",
            closes_at=int(time.time()) + 1000
        )
        
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 19, 15, 0, 0, tzinfo=timezone.utc)
        
        with patch("src.strategy.guardian.datetime", mock_dt):
            await pm.evaluate_and_trigger(
                market=market,
                side="YES",
                entry_price=0.7,
                size_usdc=5.0,  # low base size
                shares=7.14,
                strategy_mode="TREND",
                elapsed_secs=10.0,
                persistence_score=0.6,
                should_block_for_loss=False,
                active_strategy="TREND"
            )
            
            # stop_loss_filtered should NOT be in the triggered list due to the < 6.0 shares floor
            calls = [call.kwargs.get("profile_name") for call in pos_mgr_mock.open_profile_position.call_args_list]
            assert "stop_loss_filtered" not in calls

    @pytest.mark.asyncio
    async def test_guardian_pause_blocks_evaluation(self, db):
        from src.strategy.guardian import Guardian
        from datetime import datetime, timezone
        from unittest.mock import patch
        
        pos_mgr_mock = MagicMock()
        guardian = Guardian(db, pos_mgr_mock)
        await guardian.init_tables()
        
        # 1. Start with ACTIVE state
        await db.execute("INSERT OR REPLACE INTO profile_states (profile_name, state) VALUES ('stop_loss_filtered', 'ACTIVE')")
        await db.commit()
        
        # Mock datetime to a non-restricted Thursday at 15:00 UTC
        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)
        
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert await guardian.is_profile_allowed("stop_loss_filtered") is True
            
        # 2. Change state to PAUSED
        await db.execute("UPDATE profile_states SET state = 'PAUSED' WHERE profile_name = 'stop_loss_filtered'")
        await db.commit()
        
        # Should return False immediately
        with patch("src.strategy.guardian.datetime", mock_dt):
            assert await guardian.is_profile_allowed("stop_loss_filtered") is False

    @pytest.mark.asyncio
    async def test_guardian_re_arm(self):
        from src.strategy.guardian import Guardian
        from unittest.mock import patch
        import sqlite3
        import tempfile
        import os
        
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        
        # Initialize the table in the temp db using standard sqlite3
        conn = sqlite3.connect(tmp.name)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_states (
                profile_name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                current_streak INTEGER DEFAULT 0,
                daily_drawdown REAL DEFAULT 0.0,
                paused_at INTEGER,
                disabled_at INTEGER,
                last_evaluated INTEGER
            )
        """)
        conn.execute("INSERT OR REPLACE INTO profile_states (profile_name, state, paused_at, disabled_at) VALUES ('stop_loss_filtered', 'PAUSED', 1234, 5678)")
        conn.commit()
        conn.close()
        
        guardian = Guardian(MagicMock(), MagicMock())
        
        # Patch sqlite3.connect to redirect to the temp db file
        with patch("sqlite3.connect", return_value=sqlite3.connect(tmp.name)):
            guardian.re_arm("stop_loss_filtered")
            
        # Verify the database state
        conn = sqlite3.connect(tmp.name)
        cursor = conn.cursor()
        cursor.execute("SELECT state, paused_at, disabled_at FROM profile_states WHERE profile_name = 'stop_loss_filtered'")
        row = cursor.fetchone()
        assert row[0] == "ACTIVE"
        assert row[1] is None
        assert row[2] is None
        conn.close()
        os.unlink(tmp.name)






