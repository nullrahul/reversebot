"""
Unit tests for Executioner Profile & Executioner Positions Table
"""

import pytest
import aiosqlite

from src.db.queries import (
    init_db,
    init_executioner_table,
    insert_executioner_position,
    close_executioner_position,
)


@pytest.mark.asyncio
async def test_executioner_table_and_queries(tmp_path):
    db_path = str(tmp_path / "test_exec.db")
    db = await init_db(db_path)

    # 1. Initialize table
    await init_executioner_table(db)

    # 2. Insert market
    await db.execute(
        """
        INSERT INTO markets (market_id, question, slug, closes_at)
        VALUES ('m1', 'Will BTC go up in 5 minutes?', 'btc-up-5m-100', 1700000000)
        """
    )
    await db.commit()

    # 3. Insert executioner position
    pos_id = "virtual_executioner_m1"
    await insert_executioner_position(
        db,
        position_id=pos_id,
        base_profile_name="stop_loss",
        request_id=1,
        market_id="m1",
        entry_price=0.25,
        size_usdc=10.0,
        shares_bought=40.0,
        side="YES",
        strategy_mode="TREND",
    )

    # Verify insertion
    cursor = await db.execute("SELECT * FROM executioner_positions WHERE position_id = ?", (pos_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert row["base_profile_name"] == "stop_loss"
    assert row["request_id"] == 1
    assert row["market_id"] == "m1"
    assert row["entry_price"] == 0.25
    assert row["order_status"] == "OPEN"

    # 4. Close executioner position
    await close_executioner_position(
        db,
        position_id=pos_id,
        exit_price=1.0,
        realised_pnl=30.0,
        close_reason="RESOLUTION",
    )

    cursor = await db.execute("SELECT * FROM executioner_positions WHERE position_id = ?", (pos_id,))
    row_closed = await cursor.fetchone()
    assert row_closed["order_status"] == "FILLED"
    assert row_closed["exit_price"] == 1.0
    assert row_closed["realised_pnl"] == 30.0
    assert row_closed["close_reason"] == "RESOLUTION"

    await db.close()


@pytest.mark.asyncio
async def test_executioner_multi_request_concurrency(tmp_path):
    """Test that Request 1, Request 2, and Request 3 can open concurrent positions on the same market."""
    db_path = str(tmp_path / "test_exec_multi.db")
    db = await init_db(db_path)
    await init_executioner_table(db)

    # Market m1
    await db.execute("INSERT INTO markets (market_id, question, slug, closes_at) VALUES ('m1', 'BTC 5m', 'btc-5m', 1700000000)")
    await db.commit()

    # Request 1 trade
    await insert_executioner_position(
        db, position_id="virtual_executioner_m1_req1", base_profile_name="stop_loss", request_id=1,
        market_id="m1", entry_price=0.25, size_usdc=10.0, shares_bought=40.0, side="YES"
    )

    # Request 2 trade on SAME market
    await insert_executioner_position(
        db, position_id="virtual_executioner_m1_req2", base_profile_name="cross_market_hedge", request_id=2,
        market_id="m1", entry_price=0.06, size_usdc=10.0, shares_bought=166.0, side="NO"
    )

    # Request 3 trade on SAME market
    await insert_executioner_position(
        db, position_id="virtual_executioner_m1_req3", base_profile_name="reversal_probability_sl", request_id=3,
        market_id="m1", entry_price=0.65, size_usdc=10.0, shares_bought=15.0, side="YES"
    )

    cursor = await db.execute("SELECT request_id, base_profile_name FROM executioner_positions WHERE market_id = 'm1'")
    rows = await cursor.fetchall()
    assert len(rows) == 3
    req_ids = {r["request_id"] for r in rows}
    assert req_ids == {1, 2, 3}

    # Close all positions
    for r in [1, 2, 3]:
        await close_executioner_position(db, f"virtual_executioner_m1_req{r}", 1.0, 10.0, "RESOLUTION")

    cursor = await db.execute("SELECT COUNT(*) FROM executioner_positions WHERE market_id = 'm1' AND order_status = 'FILLED'")
    count = (await cursor.fetchone())[0]
    assert count == 3

    await db.close()
