import pytest
import asyncio
import tempfile
import time
import os
from unittest.mock import AsyncMock, MagicMock

from src.db.queries import init_db, close_db, upsert_market, get_open_positions
from src.strategy.position_mgr import PositionManager


@pytest.mark.asyncio
async def test_open_position_minimum_notional_clamp():
    """
    Verify that open_position clamps shares to ensure notional value >= $1.01
    across ultra-cheap prices (0.06, 0.04, 0.02, 0.01) on $1.00 base sizing.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)

    market = {
        "market_id": "mkt_clamp_1",
        "question": "BTC Up or Down",
        "category": "Crypto",
        "yes_price": 0.94,
        "no_price": 0.06,
        "yes_volume_24h": 1000.0,
        "total_volume": 5000.0,
        "closes_at": int(time.time()) + 300,
        "last_scanned_at": int(time.time()),
        "is_flagged": 0,
        "flag_reason": None,
    }
    await upsert_market(db_conn, market)

    mock_clob = AsyncMock()
    mock_clob.is_paper = False
    mock_clob.place_order = AsyncMock(return_value={"orderID": "ord_1", "status": "FILLED", "sizeMatched": 17, "price": 0.06})
    mock_clob.get_order_status = AsyncMock(return_value={"status": "FILLED", "sizeMatched": 17, "price": 0.06})
    mock_clob.get_trades_for_order = AsyncMock(return_value=[(0.06, 17.0)])
    mock_clob.get_token_balance = AsyncMock(return_value=17.0)
    
    mock_config = MagicMock()
    mock_config.paper_trade = False
    mock_config.bot_version = "v5.56"
    mock_config.hold_to_resolution = False

    pm = PositionManager(config=mock_config, clob_client=mock_clob, db=db_conn)

    # Requested shares = 16 (16 * 0.06 = $0.96, which is < $1.00)
    # The clamp should boost shares to at least 17 (17 * 0.06 = $1.02 >= $1.01)
    success = await pm.open_position(
        market_id="mkt_clamp_1",
        target_token_id="tok_1",
        entry_price=0.06,
        size_usdc=1.0,
        shares=16.0,
        side="NO"
    )
    assert success is True

    # Verify clob.place_order was called with at least 17 shares
    call_args = mock_clob.place_order.call_args[1]
    assert call_args["size"] >= 17.0
    assert call_args["price"] == 0.06
    assert call_args["size"] * call_args["price"] >= 1.01

    await close_db(db_conn)
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_open_position_delayed_status_handling():
    """
    Verify that when order placement returns status: "DELAYED",
    the polling loop waits and successfully confirms the fill once matched.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)

    market = {
        "market_id": "mkt_delay_1",
        "question": "ETH Up or Down",
        "category": "Crypto",
        "yes_price": 0.04,
        "no_price": 0.96,
        "yes_volume_24h": 1000.0,
        "total_volume": 5000.0,
        "closes_at": int(time.time()) + 300,
        "last_scanned_at": int(time.time()),
        "is_flagged": 0,
        "flag_reason": None,
    }
    await upsert_market(db_conn, market)

    mock_clob = AsyncMock()
    mock_clob.is_paper = False
    # First response returns status: "DELAYED"
    mock_clob.place_order = AsyncMock(return_value={"orderID": "ord_delay_1", "status": "DELAYED"})
    
    # Polling returns DELAYED on first poll, then MATCHED on second poll
    mock_clob.get_order_status = AsyncMock(side_effect=[
        {"status": "DELAYED"},
        {"status": "MATCHED", "sizeMatched": 26, "price": 0.04}
    ])
    mock_clob.get_trades_for_order = AsyncMock(return_value=[(0.04, 26.0)])
    mock_clob.get_token_balance = AsyncMock(return_value=26.0)
    
    mock_config = MagicMock()
    mock_config.paper_trade = False
    mock_config.bot_version = "v5.56"
    mock_config.hold_to_resolution = False

    pm = PositionManager(config=mock_config, clob_client=mock_clob, db=db_conn)

    success = await pm.open_position(
        market_id="mkt_delay_1",
        target_token_id="tok_delay_1",
        entry_price=0.04,
        size_usdc=1.0,
        shares=25.0,
        side="YES"
    )
    assert success is True
    assert mock_clob.get_order_status.call_count >= 2

    # Verify open positions record in DB
    positions = await get_open_positions(db_conn, paper=False)
    assert len(positions) == 1
    assert positions[0]["market_id"] == "mkt_delay_1"

    await close_db(db_conn)
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_open_position_blocks_when_max_exposure_reached():
    """
    Verify that PositionManager.open_position rejects new live orders if
    current live exposure + order size exceeds max_total_exposure_usdc.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)

    market = {
        "market_id": "mkt_exp_1",
        "question": "BTC Up or Down",
        "category": "Crypto",
        "yes_price": 0.06,
        "no_price": 0.94,
        "yes_volume_24h": 1000.0,
        "total_volume": 5000.0,
        "closes_at": int(time.time()) + 300,
        "last_scanned_at": int(time.time()),
        "is_flagged": 0,
        "flag_reason": None,
    }
    await upsert_market(db_conn, market)

    mock_clob = AsyncMock()
    mock_clob.is_paper = False
    
    mock_config = MagicMock()
    mock_config.paper_trade = False
    mock_config.bot_version = "v5.56"
    mock_config.max_total_exposure_usdc = 2.0  # Max $2.00 exposure

    pm = PositionManager(config=mock_config, clob_client=mock_clob, db=db_conn)

    # Insert an existing open position of $1.50 in the database
    existing_mkt = {
        "market_id": "existing_mkt",
        "question": "ETH Up or Down",
        "category": "Crypto",
        "yes_price": 0.50,
        "no_price": 0.50,
        "yes_volume_24h": 1000.0,
        "total_volume": 5000.0,
        "closes_at": int(time.time()) + 300,
        "last_scanned_at": int(time.time()),
        "is_flagged": 0,
        "flag_reason": None,
    }
    await upsert_market(db_conn, existing_mkt)
    from src.db.queries import create_position
    await create_position(db_conn, market_id="existing_mkt", entry_price=0.06, size_usdc=1.50, shares_bought=25, bot_version="v5.56", paper=False)

    # Try to open a new position of $1.00 ($1.02 notional). Total exposure would be $1.50 + $1.02 = $2.52 > $2.00
    success = await pm.open_position(
        market_id="mkt_exp_1",
        target_token_id="tok_exp_1",
        entry_price=0.06,
        size_usdc=1.0,
        shares=17.0,
        side="YES"
    )

    assert success is False
    assert not mock_clob.place_order.called, "Order must NOT be dispatched to CLOB if max exposure is exceeded"

    await close_db(db_conn)
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_open_position_blocks_when_insufficient_wallet_balance():
    """
    Verify that PositionManager.open_position rejects new live orders if
    the active wallet balance is lower than the order size.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)

    market = {
        "market_id": "mkt_bal_1",
        "question": "BTC Up or Down",
        "category": "Crypto",
        "yes_price": 0.06,
        "no_price": 0.94,
        "yes_volume_24h": 1000.0,
        "total_volume": 5000.0,
        "closes_at": int(time.time()) + 300,
        "last_scanned_at": int(time.time()),
        "is_flagged": 0,
        "flag_reason": None,
    }
    await upsert_market(db_conn, market)

    mock_clob = AsyncMock()
    mock_clob.is_paper = False
    # Mock wallet balance of $0.50 (less than $1.02 required)
    mock_clob.get_account_balance = AsyncMock(return_value={"balance": "0.500000", "allowance": "1000.0"})
    
    mock_config = MagicMock()
    mock_config.paper_trade = False
    mock_config.bot_version = "v5.56"
    mock_config.max_total_exposure_usdc = 20.0

    pm = PositionManager(config=mock_config, clob_client=mock_clob, db=db_conn)

    success = await pm.open_position(
        market_id="mkt_bal_1",
        target_token_id="tok_bal_1",
        entry_price=0.06,
        size_usdc=1.0,
        shares=17.0,
        side="YES"
    )

    assert success is False
    assert not mock_clob.place_order.called, "Order must NOT be dispatched if wallet has insufficient balance"

    await close_db(db_conn)
    os.unlink(tmp.name)

