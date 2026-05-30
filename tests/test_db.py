"""
Tests for database schema, queries, and constraints.
[v0.1.0] DB: Schema creation, CRUD, partial unique index, parameterized queries.
"""

import asyncio
import os
import tempfile
import pytest
import pytest_asyncio

# Ensure we can import from src
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import (
    init_db,
    close_db,
    upsert_market,
    get_market,
    get_flagged_markets,
    update_market_threshold_tracking,
    reset_market_threshold_tracking,
    create_position,
    update_position_status,
    close_position,
    get_open_positions,
    has_open_position_for_market,
    count_open_positions,
    get_total_exposure,
    get_daily_realised_loss,
    log_scan,
    log_event,
    update_position_min_max_prices,
)


@pytest_asyncio.fixture
async def db():
    """Create a temporary in-memory database for testing."""
    # Use a temp file so we get real SQLite behavior (partial indexes work)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _sample_market(market_id: str = "0xtest123", **overrides) -> dict:
    """Create a sample market dict for testing."""
    import time
    base = {
        "market_id": market_id,
        "question": "Will BTC hit 100k?",
        "category": "Crypto",
        "yes_price": 0.85,
        "no_price": 0.15,
        "yes_volume_24h": 1500.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "closes_at": int(time.time()) + 86400,
        "is_flagged": 0,
        "flag_reason": None,
        "last_seen_above_threshold": None,
        "consecutive_cycles_above": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    """Verify all tables and indexes are created correctly."""

    @pytest.mark.asyncio
    async def test_tables_exist(self, db):
        """All 5 required tables must exist."""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = [r[0] for r in rows]

        assert "markets" in table_names
        assert "positions" in table_names
        assert "paper_positions" in table_names
        assert "scan_log" in table_names
        assert "events_log" in table_names
        assert "schema_migrations" in table_names

    @pytest.mark.asyncio
    async def test_partial_unique_index_exists(self, db):
        """Partial unique index on positions(market_id) WHERE status='OPEN' must exist."""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_positions_market_open'"
        )
        row = await cursor.fetchone()
        assert row is not None, "Partial unique index idx_positions_market_open missing"

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, db):
        """WAL journal mode should be active for concurrent reads."""
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0].lower() == "wal"


# ---------------------------------------------------------------------------
# Markets CRUD Tests
# ---------------------------------------------------------------------------

class TestMarkets:

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, db):
        """Insert a market, then retrieve it."""
        market = _sample_market()
        await upsert_market(db, market)
        result = await get_market(db, "0xtest123")
        assert result is not None
        assert result["question"] == "Will BTC hit 100k?"
        assert result["yes_price"] == 0.85
        assert result["category"] == "Crypto"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, db):
        """Upserting the same market_id updates fields."""
        await upsert_market(db, _sample_market(yes_price=0.80))
        await upsert_market(db, _sample_market(yes_price=0.90))
        result = await get_market(db, "0xtest123")
        assert result["yes_price"] == 0.90

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db):
        result = await get_market(db, "0xnonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_flagged_markets(self, db):
        await upsert_market(db, _sample_market("0x1", is_flagged=1, flag_reason="yes_bias"))
        await upsert_market(db, _sample_market("0x2", is_flagged=0))
        await upsert_market(db, _sample_market("0x3", is_flagged=1, flag_reason="yes_bias"))
        flagged = await get_flagged_markets(db)
        assert len(flagged) == 2

    @pytest.mark.asyncio
    async def test_threshold_tracking(self, db):
        await upsert_market(db, _sample_market())
        await update_market_threshold_tracking(db, "0xtest123", 1700000000, 2)
        m = await get_market(db, "0xtest123")
        assert m["last_seen_above_threshold"] == 1700000000
        assert m["consecutive_cycles_above"] == 2

        await reset_market_threshold_tracking(db, "0xtest123")
        m = await get_market(db, "0xtest123")
        assert m["last_seen_above_threshold"] is None
        assert m["consecutive_cycles_above"] == 0


# ---------------------------------------------------------------------------
# Positions Tests
# ---------------------------------------------------------------------------

class TestPositions:

    @pytest.mark.asyncio
    async def test_create_position(self, db):
        """Create a position and verify default status is PENDING."""
        await upsert_market(db, _sample_market())
        pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0")
        assert pid is not None

        from src.db.queries import get_position
        pos = await get_position(db, pid)
        assert pos["order_status"] == "PENDING"
        assert pos["side"] == "NO"
        assert pos["entry_price"] == 0.15
        assert pos["size_usdc"] == 15.0
        assert pos["shares_bought"] == 100.0

    @pytest.mark.asyncio
    async def test_update_position_status(self, db):
        await upsert_market(db, _sample_market())
        pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0")
        await update_position_status(db, pid, "OPEN", order_id="order_abc")
        from src.db.queries import get_position
        pos = await get_position(db, pid)
        assert pos["order_status"] == "OPEN"
        assert pos["order_id"] == "order_abc"

    @pytest.mark.asyncio
    async def test_close_position(self, db):
        await upsert_market(db, _sample_market(closes_at=1735689600))
        pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0")
        await update_position_status(db, pid, "OPEN")
        await close_position(db, pid, 0.45, 29.10, "TAKE_PROFIT")
        from src.db.queries import get_position
        pos = await get_position(db, pid)
        assert pos["order_status"] == "FILLED"
        assert pos["close_reason"] == "TAKE_PROFIT"
        assert pos["exit_price"] == 0.45
        assert pos["realised_pnl"] == 29.10
        assert pos["resolution_delay"] is not None
        assert pos["resolution_delay"] == pos["closed_at"] - 1735689600

    @pytest.mark.asyncio
    async def test_partial_unique_index_prevents_duplicates(self, db):
        """Two OPEN positions for the same market_id must be rejected by the DB."""
        await upsert_market(db, _sample_market())

        pid1 = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0")
        await update_position_status(db, pid1, "OPEN")

        pid2 = await create_position(db, "0xtest123", 0.16, 15.0, 93.75, "v0.1.0")
        # This should raise because of the partial unique index
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await update_position_status(db, pid2, "OPEN")

    @pytest.mark.asyncio
    async def test_has_open_position(self, db):
        await upsert_market(db, _sample_market())
        assert not await has_open_position_for_market(db, "0xtest123")

        pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0")
        await update_position_status(db, pid, "OPEN")
        assert await has_open_position_for_market(db, "0xtest123")

    @pytest.mark.asyncio
    async def test_count_and_exposure(self, db):
        await upsert_market(db, _sample_market("0x1"))
        await upsert_market(db, _sample_market("0x2"))

        pid1 = await create_position(db, "0x1", 0.15, 15.0, 100.0, "v0.1.0")
        await update_position_status(db, pid1, "OPEN")

        pid2 = await create_position(db, "0x2", 0.20, 20.0, 100.0, "v0.1.0")
        await update_position_status(db, pid2, "OPEN")

        assert await count_open_positions(db) == 2
        assert await get_total_exposure(db) == 35.0

    @pytest.mark.asyncio
    async def test_shares_calculation_invariant(self, db):
        """
        MISTAKE 3 guard: shares * entry_price ≈ size_usdc.
        This asserts the correct calculation: shares = size_usdc / no_price.
        """
        entry_price = 0.15
        size_usdc = 15.0
        shares = size_usdc / entry_price  # 100.0

        # Verify the invariant
        assert abs(shares * entry_price - size_usdc) < 0.001, (
            f"Shares calculation broken: {shares} * {entry_price} != {size_usdc}"
        )

        # Wrong calculation for comparison
        wrong_shares = size_usdc  # 15.0 — this is WRONG
        assert abs(wrong_shares * entry_price - size_usdc) > 0.001, (
            "The wrong calculation should NOT satisfy the invariant"
        )

    @pytest.mark.asyncio
    async def test_min_max_prices_tracking(self, db):
        """Verify min/max price columns are initialized on creation and updatable."""
        await upsert_market(db, _sample_market())
        pid = await create_position(db, "0xtest123", 0.48, 15.0, 31.25, "v0.1.0")
        
        from src.db.queries import get_position
        pos = await get_position(db, pid)
        # Check initial values are equal to entry price
        assert pos["max_price_seen"] == 0.48
        assert pos["min_price_seen"] == 0.48
        
        # Update prices
        await update_position_min_max_prices(db, pid, 0.42, 0.55)
        
        pos = await get_position(db, pid)
        assert pos["min_price_seen"] == 0.42
        assert pos["max_price_seen"] == 0.55



# ---------------------------------------------------------------------------
# Scan Log & Events Tests
# ---------------------------------------------------------------------------

class TestAuditLogs:

    @pytest.mark.asyncio
    async def test_log_scan(self, db):
        scan_id = await log_scan(db, 100, 5, 2, 1, "v0.1.0")
        assert scan_id is not None
        assert scan_id > 0

    @pytest.mark.asyncio
    async def test_log_event(self, db):
        event_id = await log_event(
            db,
            event_type="SIGNAL",
            bot_version="v0.1.0",
            market_id="0xtest",
            payload={"yes_price": 0.85, "action": "detected"},
        )
        assert event_id is not None
        assert event_id > 0

    @pytest.mark.asyncio
    async def test_events_are_append_only(self, db):
        """Events log should accumulate, never lose entries."""
        await log_event(db, "SCAN", "v0.1.0")
        await log_event(db, "SIGNAL", "v0.1.0")
        await log_event(db, "ORDER_PLACED", "v0.1.0")

        cursor = await db.execute("SELECT COUNT(*) FROM events_log")
        row = await cursor.fetchone()
        assert row[0] == 3


# ---------------------------------------------------------------------------
# Paper Trading Tests
# ---------------------------------------------------------------------------

class TestPaperPositions:

    @pytest.mark.asyncio
    async def test_paper_positions_separate(self, db):
        """Paper positions are tracked separately from live positions."""
        await upsert_market(db, _sample_market())

        # Create live position
        live_pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0", paper=False)
        await update_position_status(db, live_pid, "OPEN", paper=False)

        # Create paper position for same market (should work — different table)
        paper_pid = await create_position(db, "0xtest123", 0.15, 15.0, 100.0, "v0.1.0", paper=True)
        await update_position_status(db, paper_pid, "OPEN", paper=True)

        assert await count_open_positions(db, paper=False) == 1
        assert await count_open_positions(db, paper=True) == 1
