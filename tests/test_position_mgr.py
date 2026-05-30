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
    async def test_fok_retry_success(self, db):
        """If the first FOK order is killed, it should retry at entry_price + 0.01 and succeed."""
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
        assert pos["entry_price"] == 0.46
        assert pos["size_usdc"] == round(33 * 0.46, 2)

    @pytest.mark.asyncio
    async def test_fok_cascade_retry_success_reversal(self, db):
        """Reversal trades bypass empirical gates and can retry up to max_slippage.
        This test simulates a streak_depth = 4 reversal market (max_slippage = 0.02).
        First attempt (entry_price 0.45) fails.
        Second attempt (+0.01 retry, 0.46) fails.
        Third attempt (+0.02 retry, 0.47) succeeds.
        """
        market = {
            "market_id": "0xreversal_cascade",
            "question": "Will ETH Up or Down - 5 Minutes?",
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
            "streak_depth": 4,  # Reversal active for 5m window
        }
        await upsert_market(db, market)

        config = _mock_config()
        clob = AsyncMock()
        clob.is_paper = True

        # Mock place_order to return different IDs for consecutive calls
        clob.place_order = AsyncMock(side_effect=[
            {"orderID": "order_1", "status": "KILLED"},
            {"orderID": "order_2", "status": "KILLED"},
            {"orderID": "order_3", "status": "MATCHED"}
        ])

        # Mock get_order_status to return KILLED for the first two and FILLED for the third
        clob.get_order_status = AsyncMock(side_effect=lambda order_id: {
            "order_1": {"status": "KILLED", "orderID": "order_1"},
            "order_2": {"status": "KILLED", "orderID": "order_2"},
            "order_3": {"status": "FILLED", "orderID": "order_3"}
        }.get(order_id))

        mgr = PositionManager(config, clob, db)

        # entry_price = 0.45. Since streak_depth = 4 and window = 5, is_reversal = True.
        # This allows FOK retries up to +0.02, bypassing standard price ceilings.
        success = await mgr.open_position(
            market_id="0xreversal_cascade",
            target_token_id="yes_token",
            entry_price=0.45,
            size_usdc=15.0,
            shares=33.0,
            side="YES",
            streak_depth=4
        )

        assert success is True

        # Verify position is OPEN in DB and has updated to retry_price = 0.47
        cursor = await db.execute("SELECT * FROM paper_positions WHERE market_id = ?", ("0xreversal_cascade",))
        pos = await cursor.fetchone()
        assert pos is not None
        assert pos["order_status"] == "OPEN"
        assert round(pos["entry_price"], 2) == 0.47
        assert pos["size_usdc"] == round(33 * 0.47, 2)



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


class TestFOKEnforcement:

    @pytest.mark.asyncio
    async def test_only_fok_orders_allowed(self):
        """Attempting a MARKET order should raise AssertionError."""
        from src.api.clob_client import ClobClient

        auth = MagicMock()
        auth.build_headers = MagicMock(return_value={})

        clob = ClobClient(auth, paper_trade=True)

        with pytest.raises(AssertionError, match="FORBIDDEN"):
            await clob.place_order("0xtoken", "BUY", 0.15, 100, order_type="MARKET")

    @pytest.mark.asyncio
    async def test_fok_order_allowed(self):
        """FOK order should work without assertion error."""
        from src.api.clob_client import ClobClient

        auth = MagicMock()
        auth.build_headers = MagicMock(return_value={})

        clob = ClobClient(auth, paper_trade=True)

        result = await clob.place_order("0xtoken", "BUY", 0.15, 100, order_type="FOK")
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
