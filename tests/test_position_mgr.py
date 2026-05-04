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
        "take_profit_multiplier": 3.0,
        "stop_loss_multiplier": 0.5,
        "position_size_usdc": 15.0,
        "trailing_tp_enabled": True,
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
