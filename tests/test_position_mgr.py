"""
Tests for Position Manager — open/close lifecycle, race conditions, shares math.
"""

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import (
    init_db, close_db, upsert_market, get_position,
    get_open_positions, has_open_position_for_market,
)
from src.strategy.position_mgr import PositionManager, get_market_lock


@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _mock_config(**overrides):
    defaults = {
        "paper_trade": True,
        "bot_version": "v0.1.0",
        "position_size_usdc": 15.0,
        "tp1_shares_count": 8,
        "tp1_multiplier": 1.5,
        "tp2_multiplier": 2.5,
        "timeframe_15m_enabled": True,
        "max_total_exposure_usdc": 100.0,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def _mock_clob():
    """Create a mock CLOB client that simulates successful paper trades."""
    clob = AsyncMock()
    clob.is_paper = True

    # place_order returns a successful order
    clob.place_order = AsyncMock(return_value={
        "orderID": "paper_test123",
        "status": "MATCHED",
    })

    # wait_for_fill returns MATCHED
    clob.wait_for_fill = AsyncMock(return_value="MATCHED")

    # get_order_status returns MATCHED
    clob.get_order_status = AsyncMock(return_value={"status": "MATCHED", "orderID": "paper_test123"})

    # get_current_price returns a price
    clob.get_current_price = AsyncMock(return_value=0.25)

    return clob


class TestOpenPosition:

    @pytest.mark.asyncio
    async def test_successful_open(self, db):
        """Opening a position should go PENDING → OPEN."""
        market = {
            "market_id": "0xtest",
            "question": "Test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        success = await mgr.open_position(
            market_id="0xtest",
            no_token_id="0xtest_no",
            entry_price=0.15,
            size_usdc=15.0,
            shares=100.0,
        )

        assert success is True

        # Verify position is OPEN in DB
        positions = await get_open_positions(db, paper=True)
        assert len(positions) == 1
        assert positions[0]["order_status"] == "OPEN"
        assert positions[0]["entry_price"] == 0.15

    @pytest.mark.asyncio
    async def test_failed_order_marks_failed(self, db):
        """If order placement fails, position should be FAILED."""
        market = {
            "market_id": "0xfail",
            "question": "Fail",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        clob.place_order = AsyncMock(return_value=None)  # Order rejected

        mgr = PositionManager(config, clob, db)

        success = await mgr.open_position(
            market_id="0xfail",
            no_token_id="0xfail_no",
            entry_price=0.15,
            size_usdc=15.0,
            shares=100.0,
        )

        assert success is False

    @pytest.mark.asyncio
    async def test_fak_retry_success(self, db):
        """If the first FAK order is killed, it should retry at entry_price + 0.01 and succeed."""
        market = {
            "market_id": "0xretry_test",
            "question": "Will BTC Up or Down - 5 Minutes?",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = AsyncMock()
        clob.is_paper = True
        
        # Mock place_order to return different IDs for first vs second call
        clob.place_order = AsyncMock(side_effect=[
            {"orderID": "first_order_id", "status": "KILLED"},
            {"orderID": "retry_order_id", "status": "MATCHED"}
        ])
        
        # Mock get_order_status to return KILLED for the first order, and FILLED for the retry
        clob.get_order_status = AsyncMock(side_effect=lambda order_id: {
            "first_order_id": {"status": "KILLED", "orderID": "first_order_id"},
            "retry_order_id": {"status": "FILLED", "orderID": "retry_order_id"}
        }.get(order_id))

        mgr = PositionManager(config, clob, db)

        # entry_price = 0.45, retry_price = 0.46 (passes 5m YES ceiling 0.62)
        success = await mgr.open_position(
            market_id="0xretry_test",
            target_token_id="yes_token",
            entry_price=0.45,
            size_usdc=15.0,
            shares=33.0,
            side="YES"
        )

        assert success is True

        # Verify position is OPEN in DB and has updated retry price & cost
        cursor = await db.execute("SELECT * FROM paper_positions WHERE market_id = ?", ("0xretry_test",))
        pos = await cursor.fetchone()
        assert pos is not None
        assert pos["order_status"] == "OPEN"
        assert pos["entry_price"] == 0.45
        assert pos["size_usdc"] == round(33 * 0.45, 2)

    @pytest.mark.asyncio
    async def test_fak_cascade_retry_fail_after_15_attempts(self, db):
        """Verify that FAK retry fails after 15 attempts if order is consistently killed."""
        market = {
            "market_id": "0xdeep_streak_cascade",
            "question": "Will BTC Up or Down - 15 Minutes?",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
            "streak_depth": 4,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = AsyncMock()
        clob.is_paper = True

        # Mock place_order to return KILLED consistently
        clob.place_order = AsyncMock(return_value={"orderID": "order_fail", "status": "KILLED"})

        # Mock get_order_status
        clob.get_order_status = AsyncMock(return_value={"status": "KILLED", "orderID": "order_fail"})

        mgr = PositionManager(config, clob, db)

        success = await mgr.open_position(
            market_id="0xdeep_streak_cascade",
            target_token_id="yes_token",
            entry_price=0.45,
            size_usdc=15.0,
            shares=33.0,
            side="YES",
            streak_depth=4
        )

        assert success is False
        assert clob.place_order.call_count == 15


    @pytest.mark.asyncio
    async def test_fak_retry_uses_live_best_ask(self, db):
        """Verify that FAK retry queries the live best ask from WebSocket and uses it."""
        market = {
            "market_id": "0xws_retry_test",
            "question": "Will BTC Up or Down - 5 Minutes?",
            "category": "Crypto",
            "yes_price": 0.45,
            "no_price": 0.55,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = AsyncMock()
        clob.is_paper = True
        
        # Mock place_order
        clob.place_order = AsyncMock(side_effect=[
            {"orderID": "order_1", "status": "KILLED"},
            {"orderID": "order_2", "status": "MATCHED"}
        ])
        
        clob.get_order_status = AsyncMock(side_effect=lambda order_id: {
            "order_1": {"status": "KILLED", "orderID": "order_1"},
            "order_2": {"status": "FILLED", "orderID": "order_2"}
        }.get(order_id))

        mgr = PositionManager(config, clob, db)
        
        # Mock WebSocket client
        ws = MagicMock()
        ws.get_best_ask = MagicMock(side_effect=[0.45, 0.49])
        mgr.ws = ws

        success = await mgr.open_position(
            market_id="0xws_retry_test",
            target_token_id="yes_token",
            entry_price=0.45,
            size_usdc=15.0,
            shares=33.0,
            side="YES"
        )

        assert success is True
        assert clob.place_order.call_count == 2
        clob.place_order.assert_any_call(token_id="yes_token", side="BUY", price=0.45, size=33.0, order_type="FAK")
        clob.place_order.assert_any_call(token_id="yes_token", side="BUY", price=0.49, size=30.0, order_type="FAK")



class TestRaceConditionProtection:

    @pytest.mark.asyncio
    async def test_market_lock_prevents_concurrent_opens(self, db):
        """Two concurrent open_position calls for the same market should be serialized.
        The DB partial unique index is the secondary guard and should catch any
        race condition that slips past the asyncio.Lock."""
        market = {
            "market_id": "0xrace",
            "question": "Race test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Fire two opens concurrently — one should succeed, other should fail
        # due to either the lock or the DB unique constraint
        results = await asyncio.gather(
            mgr.open_position("0xrace", "0xrace_no", 0.15, 15.0, 100.0),
            mgr.open_position("0xrace", "0xrace_no", 0.15, 15.0, 100.0),
            return_exceptions=True,  # Catch exceptions instead of propagating
        )

        # Count successful opens (True results, not exceptions)
        successes = sum(1 for r in results if r is True)

        # At most one should succeed
        assert successes <= 1, (
            f"Race condition: {successes} positions opened for same market!"
        )

        # Verify DB state — at most 1 OPEN position
        open_positions = await get_open_positions(db, paper=True)
        assert len(open_positions) <= 1, (
            f"Race condition: {len(open_positions)} OPEN positions for same market!"
        )

    @pytest.mark.asyncio
    async def test_lock_is_per_market(self, db):
        """Different markets should use separate locks."""
        lock1 = await get_market_lock("0xmarket_a")
        lock2 = await get_market_lock("0xmarket_b")

        assert lock1 is not lock2, "Different markets should have separate locks"

    @pytest.mark.asyncio
    async def test_same_market_same_lock(self):
        """Same market should return the same lock."""
        lock1 = await get_market_lock("0xsame")
        lock2 = await get_market_lock("0xsame")

        assert lock1 is lock2, "Same market should return the same lock"


class TestOrderTypeEnforcement:

    @pytest.mark.asyncio
    async def test_only_allowed_order_types(self):
        """Attempting a MARKET order should raise AssertionError."""
        from src.api.clob_client import ClobClient

        auth = MagicMock()
        auth.build_headers = MagicMock(return_value={})

        clob = ClobClient(auth, paper_trade=True)

        with pytest.raises(AssertionError, match="FORBIDDEN"):
            await clob.place_order("0xtoken", "BUY", 0.15, 100, order_type="MARKET")

    @pytest.mark.asyncio
    async def test_fak_order_allowed(self):
        """FAK order should work without assertion error."""
        from src.api.clob_client import ClobClient

        auth = MagicMock()
        auth.build_headers = MagicMock(return_value={})

        clob = ClobClient(auth, paper_trade=True)

        result = await clob.place_order("0xtoken", "BUY", 0.15, 100, order_type="FAK")
        assert result is not None
        assert "orderID" in result


class TestCheckResolutions:

    @pytest.mark.asyncio
    async def test_resolve_extreme_wicks_win(self, db):
        """If target outcome price is >= 0.98, resolve as win (1.0)."""
        market = {
            "market_id": "0xres_win",
            "question": "Resolution Win Test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) - 100,  # Expired
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Open position
        await mgr.open_position("0xres_win", "0xres_win_no", 0.15, 15.0, 100.0)

        # Mock Gamma API
        mgr.gamma = AsyncMock()
        mgr.gamma._get = AsyncMock(return_value={
            "outcomePrices": '["0.0005", "0.9995"]',
            "outcomes": '["YES", "NO"]',
        })

        # Run resolution check
        await mgr.check_resolutions()

        # Position should be closed now (not in open positions)
        open_positions = await get_open_positions(db, paper=True)
        assert len(open_positions) == 0

        # Retrieve position and verify PnL and exit price
        cursor = await db.execute("SELECT * FROM paper_positions WHERE market_id = ?", ("0xres_win",))
        pos = await cursor.fetchone()
        assert pos is not None
        assert pos["order_status"] == "FILLED"
        assert pos["exit_price"] == 1.0
        assert pos["realised_pnl"] == 85.0  # 100 shares * 1.0 - 15.0 size

    @pytest.mark.asyncio
    async def test_resolve_extreme_wicks_loss(self, db):
        """If target outcome price is <= 0.02, resolve as loss (0.0)."""
        market = {
            "market_id": "0xres_loss",
            "question": "Resolution Loss Test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) - 100,  # Expired
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Open position
        await mgr.open_position("0xres_loss", "0xres_loss_no", 0.15, 15.0, 100.0)

        # Mock Gamma API
        mgr.gamma = AsyncMock()
        mgr.gamma._get = AsyncMock(return_value={
            "outcomePrices": '["0.9995", "0.0005"]',
            "outcomes": '["YES", "NO"]',
        })

        # Run resolution check
        await mgr.check_resolutions()

        # Position should be closed now
        open_positions = await get_open_positions(db, paper=True)
        assert len(open_positions) == 0

        # Retrieve position and verify loss
        cursor = await db.execute("SELECT * FROM paper_positions WHERE market_id = ?", ("0xres_loss",))
        pos = await cursor.fetchone()
        assert pos is not None
        assert pos["order_status"] == "FILLED"
        assert pos["exit_price"] == 0.0
        assert pos["realised_pnl"] == -15.0  # 100 shares * 0.0 - 15.0 size


class TestOrderStatusCasing:

    @pytest.mark.asyncio
    async def test_lowercase_status_check_open_position(self, db):
        """Verify that lowercase order status 'matched' is correctly treated as filled during open."""
        market = {
            "market_id": "0xcase",
            "question": "Case Casing Test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        # Mock lowercase status returned by CLOB API
        clob.get_order_status = AsyncMock(return_value={"status": "matched", "orderID": "paper_test123"})
        clob.place_order = AsyncMock(return_value={"orderID": "paper_test123", "status": "matched"})

        mgr = PositionManager(config, clob, db)

        success = await mgr.open_position(
            market_id="0xcase",
            no_token_id="0xcase_no",
            entry_price=0.15,
            size_usdc=15.0,
            shares=100.0,
        )

        assert success is True

        # Verify position is OPEN in DB (which indicates status check passed)
        positions = await get_open_positions(db, paper=True)
        assert len(positions) == 1
        assert positions[0]["order_status"] == "OPEN"


class TestThinBookProtection:

    @pytest.mark.asyncio
    async def test_open_position_retries_and_aborts_on_099_price(self, db):
        """If price is >= 0.99, wait 2s, retry. If still >= 0.99, abort."""
        import unittest.mock
        market = {
            "market_id": "0xthin_abort",
            "question": "Thin Abort Test",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Mock WebSocket client to return 0.99 both times
        ws = MagicMock()
        ws.get_best_ask = MagicMock(return_value=0.99)
        mgr.ws = ws

        # Mock asyncio.sleep to avoid waiting 2 seconds in tests
        with unittest.mock.patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            success = await mgr.open_position(
                market_id="0xthin_abort",
                target_token_id="yes_token",
                entry_price=0.50,
                size_usdc=15.0,
                shares=30.0,
                side="YES"
            )

        assert success is False
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(2.0)
        # Verify clob.place_order was never called
        clob.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_position_retries_and_succeeds_on_price_stabilization(self, db):
        """If price is >= 0.99, wait 2s, retry. If price drops, succeed."""
        import unittest.mock
        market = {
            "market_id": "0xthin_recover",
            "question": "Thin Recover Test",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Mock WebSocket to return 0.99 on first call, then 0.50 on subsequent calls
        ws = MagicMock()
        ws.get_best_ask = MagicMock(side_effect=[0.99, 0.50, 0.50, 0.50])
        mgr.ws = ws

        with unittest.mock.patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            success = await mgr.open_position(
                market_id="0xthin_recover",
                target_token_id="yes_token",
                entry_price=0.50,
                size_usdc=15.0,
                shares=30.0,
                side="YES"
            )

        assert success is True
        assert mock_sleep.call_count >= 1  # At least the pre-flight sleep
        # Verify clob.place_order was called at the recovered price 0.50
        clob.place_order.assert_called_with(
            token_id="yes_token",
            side="BUY",
            price=0.50,
            size=30.0,
            order_type="FAK"
        )

    @pytest.mark.asyncio
    async def test_cascade_loop_aborts_mid_fill_on_099_price(self, db):
        """If price is low first but jumps to >= 0.99 mid-fill, cascade loop aborts keeping partial fills."""
        import unittest.mock
        market = {
            "market_id": "0xmid_abort",
            "question": "Mid Abort Test",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = AsyncMock()
        clob.is_paper = True

        # First placement: succeeds, returns MATCHED with partial fill
        clob.place_order = AsyncMock(return_value={
            "orderID": "order_part1",
            "status": "MATCHED",
            "sizeMatched": "10.0"
        })
        
        # Mock status check to return MATCHED with 10.0 shares
        clob.get_order_status = AsyncMock(return_value={
            "status": "MATCHED",
            "orderID": "order_part1",
            "sizeMatched": "10.0",
            "price": "0.50"
        })

        mgr = PositionManager(config, clob, db)

        # Mock WebSocket to return 0.50 first, then 0.99 for next check
        ws = MagicMock()
        ws.get_best_ask = MagicMock(side_effect=[0.50, 0.99, 0.99])
        mgr.ws = ws

        with unittest.mock.patch("asyncio.sleep", AsyncMock()):
            success = await mgr.open_position(
                market_id="0xmid_abort",
                target_token_id="yes_token",
                entry_price=0.50,
                size_usdc=15.0,
                shares=30.0,
                side="YES"
            )

        # Since it got a partial fill of 10.0 shares before aborting, success should be True (keeps position open with partial fill)
        assert success is True
        # Verify clob.place_order was only called once (for the first attempt before the price jumped to 0.99)
        assert clob.place_order.call_count == 1
        clob.place_order.assert_called_once_with(
            token_id="yes_token",
            side="BUY",
            price=0.50,
            size=30.0,
            order_type="FAK"
        )
        
        # Verify the position is stored with 10.0 shares in the database
        positions = await get_open_positions(db, paper=True)
        assert len(positions) == 1
        assert positions[0]["shares_bought"] == 10
        assert positions[0]["size_usdc"] == 5.0  # 10.0 shares * 0.50 price


class TestTakeProfitAndHoldingsPolling:

    @pytest.mark.asyncio
    async def test_get_confirmed_token_balance_stability(self, db):
        """Should return the balance once two consecutive reads match."""
        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)
        mgr._paper = False

        # Mock get_token_balance to return fluctuating values then stable ones
        clob.get_token_balance = AsyncMock(side_effect=[10.0, 12.0, 12.0])

        from unittest.mock import patch
        with patch("asyncio.sleep", AsyncMock()):
            balance = await mgr._get_confirmed_token_balance("token_abc", max_wait=30.0)

        assert balance == 12.0
        assert clob.get_token_balance.call_count == 3

    @pytest.mark.asyncio
    async def test_open_position_live_mode_holding_correction(self, db):
        """Should poll and correct shares bought with live holdings confirmed balance."""
        market = {
            "market_id": "0xopen_live",
            "question": "Live Test",
            "category": "Crypto",
            "yes_price": 0.85,
            "no_price": 0.15,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        clob.place_order = AsyncMock(return_value={"orderID": "live_order", "status": "MATCHED", "sizeMatched": "30.0"})
        clob.get_order_status = AsyncMock(return_value={"status": "MATCHED", "orderID": "live_order", "sizeMatched": "30.0"})

        mgr = PositionManager(config, clob, db)
        mgr._paper = False  # Simulate live

        # Mock balance check stability helper to return corrected shares count
        mgr._get_confirmed_token_balance = AsyncMock(return_value=25.0)

        from unittest.mock import patch
        with patch("asyncio.sleep", AsyncMock()):
            success = await mgr.open_position(
                market_id="0xopen_live",
                target_token_id="yes_token",
                entry_price=0.50,
                size_usdc=15.0,
                shares=30.0,
                side="YES"
            )

        assert success is True
        positions = await get_open_positions(db, paper=False)
        assert len(positions) == 1
        # Should be corrected to 25.0 instead of 30.0!
        assert positions[0]["shares_bought"] == 25.0
        assert positions[0]["size_usdc"] == 15.0  # Cost remains 15.0 (invariant), entry price is updated to 15.0 / 25.0 = 0.60

    @pytest.mark.asyncio
    async def test_take_profit_triggered_monitor(self, db):
        """Should trigger Take Profit when price crosses threshold."""
        market = {
            "market_id": "0xtp_market",
            "question": "TP Test",
            "category": "Crypto",
            "yes_price": 0.05,
            "no_price": 0.95,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(hold_to_resolution=False, take_profit_threshold=0.95)
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Create an open position in DB
        import json
        notes = json.dumps({"tid": "yes_token"})
        from src.db.queries import create_position, update_position_status
        pid = await create_position(db, "0xtp_market", entry_price=0.50, size_usdc=10.0, shares_bought=20.0, bot_version="v1.0", paper=True, notes=notes, side="YES")
        await update_position_status(db, pid, "OPEN", "paper_test", paper=True)

        # Mock WebSocket price feed
        ws = MagicMock()
        ws.get_best_bid = MagicMock(return_value=0.96)  # Above 0.95 threshold
        ws.get_best_ask = MagicMock(return_value=None)
        ws.get_live_price = MagicMock(return_value=None)
        mgr.ws = ws

        mgr._close_position = AsyncMock(return_value=True)

        await mgr.monitor_positions()

        # Verify close_position was called with TAKE_PROFIT
        mgr._close_position.assert_called_once()
        args, kwargs = mgr._close_position.call_args
        assert args[1] == 0.96
        assert args[2] == "TAKE_PROFIT"

    @pytest.mark.asyncio
    async def test_close_position_liquidates_shares_bag(self, db):
        """Should attempt exit and record shares bag when dust remaining."""
        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)
        mgr._paper = False

        # Mock initial balance fetch to return 4.2 shares (dust / bag)
        mgr._get_confirmed_token_balance = AsyncMock(return_value=4.2)
        clob.place_order = AsyncMock(return_value={"orderID": "sell_order", "status": "MATCHED", "sizeMatched": "4.2"})
        clob.get_order_status = AsyncMock(return_value={"status": "MATCHED", "orderID": "sell_order", "sizeMatched": "4.2"})

        import json
        pos = {
            "position_id": "test_close_pid",
            "market_id": "0xclose_market",
            "shares_bought": 100.0,
            "size_usdc": 50.0,
            "notes": json.dumps({"tid": "token_xyz"})
        }

        from unittest.mock import patch
        with patch("asyncio.sleep", AsyncMock()):
            success = await mgr._close_position(pos, 0.90, "TAKE_PROFIT")

        assert success is True
        # Verify clob.place_order was called with 4.2 shares (slippage 0.01 on attempt 1 -> 0.90 - 0.01 = 0.89)
        clob.place_order.assert_called_once_with("token_xyz", "SELL", 0.89, 4.2, "FAK")

    @pytest.mark.asyncio
    async def test_trailing_tp_arming_and_updates(self, db):
        """Should arm trailing TP and update peak when price goes up, and trigger when it drops."""
        market = {
            "market_id": "0xtttp_market",
            "question": "TTP Test",
            "category": "Crypto",
            "yes_price": 0.05,
            "no_price": 0.95,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(
            trailing_tp_enabled=True,
            trailing_tp_trigger=0.80,
            trailing_tp_trail=0.05,
            hold_to_resolution=False
        )
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        import json
        notes = json.dumps({"tid": "yes_token"})
        from src.db.queries import create_position, update_position_status, get_position
        pid = await create_position(db, "0xtttp_market", entry_price=0.50, size_usdc=10.0, shares_bought=20.0, bot_version="v1.0", paper=True, notes=notes, side="YES")
        await update_position_status(db, pid, "OPEN", "paper_test", paper=True)

        # 1. Price is below trigger: should NOT arm TTP
        ws = MagicMock()
        ws.get_best_bid.return_value = 0.75
        ws.get_best_ask.return_value = None
        ws.get_live_price.return_value = None
        mgr.ws = ws
        mgr._close_position = AsyncMock(return_value=True)

        await mgr.monitor_positions()
        assert mgr._trailing_tp_active.get(pid) is not True
        mgr._close_position.assert_not_called()

        # 2. Price hits trigger ($0.82): should arm TTP and set peak
        ws.get_best_bid.return_value = 0.82
        await mgr.monitor_positions()
        assert mgr._trailing_tp_active.get(pid) is True
        assert mgr._trailing_tp_peak.get(pid) == 0.82
        mgr._close_position.assert_not_called()

        # Verify state persisted in notes
        pos_db = await get_position(db, pid, paper=True)
        db_notes = json.loads(pos_db["notes"])
        assert db_notes.get("ttp_active") is True
        assert db_notes.get("ttp_peak") == 0.82

        # 3. Price goes up to $0.88: peak should update to 0.88
        ws.get_best_bid.return_value = 0.88
        await mgr.monitor_positions()
        assert mgr._trailing_tp_peak.get(pid) == 0.88
        mgr._close_position.assert_not_called()

        # 4. Price drops to $0.85 (stop is 0.88 - 0.05 = 0.83): should NOT exit
        ws.get_best_bid.return_value = 0.85
        await mgr.monitor_positions()
        mgr._close_position.assert_not_called()

        # 5. Price drops to $0.83: should trigger trailing exit!
        ws.get_best_bid.return_value = 0.83
        await mgr.monitor_positions()
        mgr._close_position.assert_called_once()
        args, kwargs = mgr._close_position.call_args
        assert args[1] == 0.83
        assert args[2] == "TRAILING_TP"

    @pytest.mark.asyncio
    async def test_core_stop_loss_trigger(self, db):
        """Should trigger stop loss exit for core position when price drops below 20%."""
        market = {
            "market_id": "0xcore_sl_market",
            "question": "Core SL Test",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(
            trailing_tp_enabled=False,
            hold_to_resolution=False
        )
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        import json
        notes = json.dumps({"tid": "yes_token"})
        from src.db.queries import create_position, update_position_status
        pid = await create_position(db, "0xcore_sl_market", entry_price=0.50, size_usdc=10.0, shares_bought=20.0, bot_version="v1.0", paper=True, notes=notes, side="YES")
        await update_position_status(db, pid, "OPEN", "paper_test", paper=True)

        ws = MagicMock()
        ws.get_best_bid.return_value = 0.45
        ws.get_best_ask.return_value = None
        ws.get_live_price.return_value = None
        mgr.ws = ws
        mgr._close_position = AsyncMock(return_value=True)

        # 1. Price is at 0.45 (above 0.40): should NOT trigger stop loss
        await mgr.monitor_positions()
        mgr._close_position.assert_not_called()

        # 2. Price drops to 0.39 (below 0.40): should trigger stop loss!
        ws.get_best_bid.return_value = 0.39
        await mgr.monitor_positions()
        mgr._close_position.assert_called_once()
        args, kwargs = mgr._close_position.call_args
        assert args[1] == 0.39
        assert args[2] == "STOP_LOSS"

    @pytest.mark.asyncio
    async def test_monitoring_price_fallback(self, db):
        """Should utilize database market price or entry price as fallback when WebSocket/REST pricing fails."""
        market = {
            "market_id": "0xfallback_market",
            "question": "Fallback Test",
            "category": "Crypto",
            "yes_price": 0.95,
            "no_price": 0.05,  # NO price has dropped to 0.05
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(
            trailing_tp_enabled=False,
            hold_to_resolution=False
        )
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        import json
        notes = json.dumps({"tid": "no_token"})
        from src.db.queries import create_position, update_position_status
        pid = await create_position(db, "0xfallback_market", entry_price=0.50, size_usdc=10.0, shares_bought=20.0, bot_version="v1.0", paper=True, notes=notes, side="NO")
        await update_position_status(db, pid, "OPEN", "paper_test", paper=True)

        # Mock WebSocket to return None
        ws = MagicMock()
        ws.get_best_bid.return_value = None
        ws.get_best_ask.return_value = None
        ws.get_live_price.return_value = None
        mgr.ws = ws
        mgr._close_position = AsyncMock(return_value=True)

        # Mock CLOB client get_current_price to return None
        clob.get_current_price = AsyncMock(return_value=None)

        # When we run monitor_positions, it should fall back to DB market price (no_price = 0.05)
        # 0.05 is below the 20% stop loss threshold (0.50 * 0.80 = 0.40) -> triggers Stop Loss!
        await mgr.monitor_positions()
        mgr._close_position.assert_called_once()
        args, kwargs = mgr._close_position.call_args
        assert args[1] == 0.05
        assert args[2] == "STOP_LOSS"

    @pytest.mark.asyncio
    async def test_static_stop_loss_profile_claude_audit(self, db):
        """Verify static 15% stop loss rules are applied correctly for 'claude_audit' virtual profile."""
        from src.db.queries import create_profile_position
        import json

        # Entry price = 0.25 -> 15% SL triggers at <= 0.2125
        market = {
            "market_id": "0xclaude_cheap",
            "question": "Cheap Test",
            "category": "Crypto",
            "yes_price": 0.75,
            "no_price": 0.25,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(trailing_tp_enabled=False, hold_to_resolution=False)
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Mock price to return 0.22 (should NOT stop out yet at 15%)
        mgr.ws = MagicMock()
        mgr.ws.get_live_price.return_value = 0.22
        clob.get_current_price = AsyncMock(return_value=0.22)

        pid_cheap = await create_profile_position(
            db, "0xclaude_cheap", entry_price=0.25, size_usdc=10.0, shares_bought=40.0,
            bot_version="v1.0", notes=json.dumps({"tid": "no_token"}), side="NO",
            profile_name="claude_audit"
        )

        await mgr.monitor_positions()
        # Verify position is still open
        cursor = await db.execute("SELECT order_status FROM profile_positions WHERE position_id = ?", (pid_cheap,))
        row = await cursor.fetchone()
        assert row["order_status"] == "OPEN"

        # Now mock price to 0.21 (hits 15% SL: <= 0.2125)
        mgr.ws.get_live_price.return_value = 0.21
        clob.get_current_price = AsyncMock(return_value=0.21)
        await mgr.monitor_positions()
        
        cursor = await db.execute("SELECT order_status, exit_price, close_reason FROM profile_positions WHERE position_id = ?", (pid_cheap,))
        row = await cursor.fetchone()
        assert row["order_status"] == "FILLED"
        assert row["exit_price"] == 0.21
        assert row["close_reason"] == "STOP_LOSS"

    @pytest.mark.asyncio
    async def test_static_stop_loss_profile_cross_market_hedge(self, db):
        """Verify static 17% stop loss rules are applied correctly for 'cross_market_hedge' virtual profile."""
        from src.db.queries import create_profile_position
        import json

        # Entry price = 0.50 -> 17% SL triggers at <= 0.415
        market = {
            "market_id": "0xcross_hedge_test",
            "question": "Hedge Test",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": int(time.time()) + 86400,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config(trailing_tp_enabled=False, hold_to_resolution=False)
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        # Mock price to return 0.42 (should NOT stop out yet at 17%)
        mgr.ws = MagicMock()
        mgr.ws.get_live_price.return_value = 0.42
        clob.get_current_price = AsyncMock(return_value=0.42)

        pid = await create_profile_position(
            db, "0xcross_hedge_test", entry_price=0.50, size_usdc=10.0, shares_bought=20.0,
            bot_version="v1.0", notes=json.dumps({"tid": "yes_token"}), side="YES",
            profile_name="cross_market_hedge"
        )

        await mgr.monitor_positions()
        # Verify position is still open
        cursor = await db.execute("SELECT order_status FROM profile_positions WHERE position_id = ?", (pid,))
        row = await cursor.fetchone()
        assert row["order_status"] == "OPEN"

        # Now mock price to 0.41 (hits 17% SL: <= 0.415)
        mgr.ws.get_live_price.return_value = 0.41
        clob.get_current_price = AsyncMock(return_value=0.41)
        await mgr.monitor_positions()
        
        cursor = await db.execute("SELECT order_status, exit_price, close_reason FROM profile_positions WHERE position_id = ?", (pid,))
        row = await cursor.fetchone()
        assert row["order_status"] == "FILLED"
        assert row["exit_price"] == 0.41
        assert row["close_reason"] == "STOP_LOSS"

    @pytest.mark.asyncio
    async def test_orphaned_skipped_position_resolves_via_gamma_with_historical_timestamp(self, db):
        """Verify check_resolutions resolves an orphaned skipped_positions row when market_id is missing from markets."""
        import json
        from src.db.skip_db import LossMomentumSkipDb

        tmp_skip = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp_skip.close()
        skip_db = LossMomentumSkipDb(tmp_skip.name)
        await skip_db.init()
        try:
            # Record a skip for a market_id NOT present in `markets` table, opened > 900s ago
            skip_id = await skip_db.record_skip(
                market_id="3945406",
                question="Bitcoin Up or Down",
                side="NO",
                entry_price=0.45,
                size_usdc=9.90,
                shares_bought=22.0,
                trigger_position_id="loss_trigger_1",
                symbol="BTC",
            )
            historical_open = int(time.time()) - 3600
            await skip_db._db.execute(
                "UPDATE skipped_positions SET opened_at = ? WHERE position_id = ?",
                (historical_open, skip_id),
            )
            await skip_db._db.commit()

            gamma = AsyncMock()
            gamma._get = AsyncMock(return_value={
                "id": "3945406",
                "slug": "btc-updown-15m-1787978700",
                "outcomes": json.dumps(["Up", "Down"]),
                "outcomePrices": json.dumps(["1", "0"]),
            })

            config = _mock_config()
            clob = _mock_clob()
            mgr = PositionManager(config, clob, db, gamma_client=gamma, skip_db=skip_db)
            await mgr.check_resolutions()

            unresolved = await skip_db.get_unresolved_skips()
            assert len(unresolved) == 0

            last_closed = await skip_db.get_last_closed_skipped_position()
            assert last_closed is not None
            assert last_closed["position_id"] == skip_id
            assert last_closed["realised_pnl"] == pytest.approx(-9.90)
            # Historical close timestamp extracted from slug: 1787978700 + 15*60 = 1787979600
            assert last_closed["closed_at"] == 1787979600
        finally:
            await skip_db.close()
            os.unlink(tmp_skip.name)

    @pytest.mark.asyncio
    async def test_concurrent_fetch_all_and_writes_no_cursor_reset_error(self, db):
        """Verify concurrent fetch_all/fetch_one and locked writes on shared aiosqlite connection never raise Cursor needed to be reset."""
        from src.db.queries import fetch_all, fetch_one, upsert_market

        async def writer(i: int):
            for j in range(10):
                await upsert_market(db, {
                    "market_id": f"mkt_{i}_{j}",
                    "question": "Bitcoin Up or Down",
                    "category": "Crypto",
                    "yes_price": 0.50,
                    "no_price": 0.50,
                    "closes_at": int(time.time()) + 300,
                })

        async def reader():
            for _ in range(20):
                rows = await fetch_all(db, "SELECT market_id, yes_price FROM markets LIMIT 5")
                one = await fetch_one(db, "SELECT COUNT(*) AS cnt FROM markets")
                assert one is not None
                assert isinstance(rows, list)

        await asyncio.gather(writer(1), writer(2), reader(), reader())

    @pytest.mark.asyncio
    async def test_probability_sl_loop_evicts_expired_positions(self, db):
        """Verify _load_open_probability_sl_positions and _monitor_probability_sl_loop evict expired positions."""
        import json
        from src.db.queries import create_profile_position

        expired_close = int(time.time()) - 60
        market = {
            "market_id": "0xprob_expired",
            "question": "Bitcoin Up or Down",
            "category": "Crypto",
            "yes_price": 0.55,
            "no_price": 0.45,
            "yes_volume_24h": 1000.0,
            "total_volume": 5000.0,
            "closes_at": expired_close,
            "last_scanned_at": int(time.time()),
            "is_flagged": 0,
            "flag_reason": None,
            "last_seen_above_threshold": None,
            "consecutive_cycles_above": 0,
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = _mock_clob()
        mgr = PositionManager(config, clob, db)

        pid = await create_profile_position(
            db, "0xprob_expired", entry_price=0.55, size_usdc=11.0, shares_bought=20.0,
            bot_version="v1.0", notes=json.dumps({"tid": "yes_token"}), side="YES",
            profile_name="probability_sl"
        )
        # Manually insert into open_probability_sl_positions with expired closes_at
        mgr.open_probability_sl_positions[pid] = {
            "position_id": pid,
            "market_id": "0xprob_expired",
            "side": "YES",
            "entry_price": 0.55,
            "size_usdc": 11.0,
            "shares_bought": 20.0,
            "order_status": "OPEN",
            "profile_name": "probability_sl",
            "notes": json.dumps({"tid": "yes_token"}),
            "closes_at": expired_close,
        }
        mgr._prob_sl_first_dip[pid] = {"start_time": time.time() - 1.0, "trigger_price": 0.45}

        # Run _load_open_probability_sl_positions (should skip loading expired row) and run 1 iteration of monitor loop
        task = asyncio.create_task(mgr._monitor_probability_sl_loop())
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert pid not in mgr.open_probability_sl_positions
        assert pid not in mgr._prob_sl_first_dip

