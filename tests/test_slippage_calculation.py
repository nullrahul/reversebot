import pytest
import os
import tempfile
import sqlite3
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock

from config.config import BotConfig
from src.api.clob_client import ClobClient
from src.strategy.position_mgr import PositionManager
from src.db.queries import init_db, close_db

@pytest_asyncio.fixture
async def db():
    """Create a temporary in-memory database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)

class MockWS:
    def __init__(self):
        self.book_cache = {}
        self.ask_cache = {}
        self.bid_cache = {}

def test_paper_order_buy_sweep():
    # Setup auth mock
    auth = MagicMock()
    clob = ClobClient(auth=auth, clob_api_url="http://fake", paper_trade=True)
    
    # Mock WS cache
    ws = MockWS()
    ws.ask_cache = {"token123": 0.50}
    ws.book_cache = {
        "token123": {
            "asks": {
                "0.50": "10.0",
                "0.51": "10.0",
                "0.52": "10.0"
            }
        }
    }
    clob.ws = ws

    # BUY order of 15 shares up to 0.52 limit
    # Should sweep: 10 shares at 0.50, and 5 shares at 0.51
    # Cost: 10 * 0.50 + 5 * 0.51 = 5.0 + 2.55 = 7.55
    # VWAP: 7.55 / 15 = 0.5033333...
    res = clob._simulate_paper_order(
        token_id="token123",
        side="BUY",
        price=0.52,
        size=15,
        order_type="FAK"
    )

    assert res["status"] == "MATCHED"
    assert res["size_matched"] == "15"
    assert abs(res["price"] - 0.5033) < 0.0001


def test_paper_order_sell_sweep():
    # Setup auth mock
    auth = MagicMock()
    clob = ClobClient(auth=auth, clob_api_url="http://fake", paper_trade=True)
    
    # Mock WS cache
    ws = MockWS()
    ws.bid_cache = {"token123": 0.45}
    ws.book_cache = {
        "token123": {
            "bids": {
                "0.45": "10.0",
                "0.44": "10.0",
                "0.43": "10.0"
            }
        }
    }
    clob.ws = ws

    # SELL order of 15 shares down to 0.43 limit
    # Should sweep: 10 shares at 0.45, and 5 shares at 0.44
    # Cost: 10 * 0.45 + 5 * 0.44 = 4.5 + 2.2 = 6.7
    # VWAP: 6.7 / 15 = 0.4466666...
    res = clob._simulate_paper_order(
        token_id="token123",
        side="SELL",
        price=0.43,
        size=15,
        order_type="FAK"
    )

    assert res["status"] == "MATCHED"
    assert res["size_matched"] == "15"
    assert abs(res["price"] - 0.4467) < 0.0001


@pytest.mark.asyncio
async def test_get_actual_fills_from_trades_success(db):
    config = MagicMock()
    config.paper_trade = False
    config.poly_address = "0xuser"
    
    clob = AsyncMock()
    clob.is_paper = False
    
    # Mock clob._request to return trades endpoint response
    # It returns a tuple of (status, data)
    mocked_trades_data = {
        "data": [
            {
                "taker_order_id": "order_abc",
                "size": "10.0",
                "price": "0.20"
            },
            {
                "taker_order_id": "order_abc",
                "size": "15.0",
                "price": "0.22"
            },
            {
                "taker_order_id": "order_other",
                "size": "5.0",
                "price": "0.25"
            }
        ]
    }
    clob._request = AsyncMock(return_value=(200, mocked_trades_data))

    mgr = PositionManager(config=config, clob_client=clob, db=db)
    
    res = await mgr._get_actual_fills_from_trades("order_abc")
    assert res is not None
    vwap, total_shares = res
    assert abs(total_shares - 25.0) < 0.0001
    # Cost: 10 * 0.20 + 15 * 0.22 = 2.0 + 3.3 = 5.3
    # VWAP: 5.3 / 25 = 0.212
    assert abs(vwap - 0.212) < 0.0001


@pytest.mark.asyncio
async def test_get_actual_fills_from_trades_not_found(db):
    config = MagicMock()
    config.paper_trade = False
    config.poly_address = "0xuser"
    
    clob = AsyncMock()
    clob.is_paper = False
    clob._request = AsyncMock(return_value=(200, {"data": []}))

    mgr = PositionManager(config=config, clob_client=clob, db=db)
    
    res = await mgr._get_actual_fills_from_trades("order_nonexistent")
    assert res is None
