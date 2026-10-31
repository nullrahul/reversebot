import os
import asyncio
import pytest
import pytest_asyncio
import tempfile
import time
import json
from unittest.mock import patch, AsyncMock, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market
from src.strategy.profiles import ProfileManager
from src.strategy.position_mgr import PositionManager
from config.config import BotConfig

@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)

def _sample_market(market_id: str = "0xtest_prob_sl", **overrides) -> dict:
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

@pytest.mark.asyncio
async def test_probability_sl_monitoring_and_exit(db):
    config = MagicMock()
    config.paper_trade = True
    config.bot_version = "test-v1.0"
    
    # Mock WS
    ws_mock = MagicMock()
    # Cache returns
    prices = {"yes_token_123": 0.55}
    ws_mock.get_best_bid = lambda tid: prices.get(tid)
    ws_mock.get_best_ask = lambda tid: prices.get(tid)
    ws_mock.get_live_price = lambda tid: prices.get(tid)
    
    # Setup PositionManager
    pos_mgr = PositionManager(config, MagicMock(), db, ws_client=ws_mock)
    pos_mgr.open_probability_sl_positions = {}
    pos_mgr._prob_sl_first_dip = {}
    
    # Insert sample market
    market = _sample_market()
    await upsert_market(db, market)
    
    # Trigger a trade in probability_sl (entry price 0.55 > 0.50)
    success = await pos_mgr.open_profile_position(
        profile_name="probability_sl",
        market_id=market["market_id"],
        target_token_id=market["yes_token_id"],
        entry_price=0.55,
        size_usdc=10.0,
        shares=18.0,
        side="YES"
    )
    assert success is True
    assert len(pos_mgr.open_probability_sl_positions) == 1
    pos_id = list(pos_mgr.open_probability_sl_positions.keys())[0]
    
    # Spawn the loop task
    monitor_task = asyncio.create_task(pos_mgr._monitor_probability_sl_loop())
    await asyncio.sleep(0.05) # Let it load the open positions
    
    # Check that it's still open
    assert pos_id in pos_mgr.open_probability_sl_positions
    assert pos_id not in pos_mgr._prob_sl_first_dip
    
    # 1. Simulate price dropping below 0.50 (e.g. 0.48)
    prices["yes_token_123"] = 0.48
    await asyncio.sleep(0.15) # Wait for the next loop tick (100ms)
    
    # It should have registered the first dip
    assert pos_id in pos_mgr._prob_sl_first_dip
    
    # Simulate price recovering back above 0.50 before 400ms is met (e.g. at 200ms)
    await asyncio.sleep(0.1) # total 250ms since first dip
    prices["yes_token_123"] = 0.52
    await asyncio.sleep(0.15) # Wait for check
    
    # It should have cleared the dip and logged a recovery wick event in db
    assert pos_id not in pos_mgr._prob_sl_first_dip
    assert pos_id in pos_mgr.open_probability_sl_positions # Still open!
    
    # Verify wick event was recorded in DB
    cursor = await db.execute("SELECT * FROM wick_events WHERE position_id = ?", (pos_id,))
    wicks = await cursor.fetchall()
    assert len(wicks) == 1
    assert wicks[0]["resolved_by"] == "RECOVERY"
    
    # 2. Now, drop price below 0.50 again (e.g. 0.45)
    prices["yes_token_123"] = 0.45
    await asyncio.sleep(0.15) # next tick
    assert pos_id in pos_mgr._prob_sl_first_dip
    
    # Wait more than 400ms (e.g. 500ms sleep)
    await asyncio.sleep(0.5)
    
    # The stop loss should have triggered and closed the position!
    assert pos_id not in pos_mgr.open_probability_sl_positions
    assert pos_id not in pos_mgr._prob_sl_first_dip
    
    # Verify the position is closed in the database
    cursor = await db.execute("SELECT * FROM profile_positions WHERE position_id = ?", (pos_id,))
    pos_row = dict(await cursor.fetchone())
    assert pos_row["order_status"] == "FILLED"
    assert pos_row["close_reason"] == "STOP_LOSS"
    
    # Verify a second wick event was recorded in DB with resolution STOP_LOSS
    cursor = await db.execute("SELECT * FROM wick_events WHERE position_id = ? AND resolved_by = 'STOP_LOSS'", (pos_id,))
    wicks2 = await cursor.fetchall()
    assert len(wicks2) == 1
    assert wicks2[0]["duration_ms"] >= 400.0
    
    # Clean up
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
