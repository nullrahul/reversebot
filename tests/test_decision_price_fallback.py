import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from src.strategy.decision import DecisionEngine


class DummyWS:
    def __init__(self):
        self.ask_cache = {}
        self.bid_cache = {}
        self.book_cache = {}

    def is_connected(self):
        return True

    def get_best_ask(self, token_id):
        return self.ask_cache.get(token_id)

    def get_best_bid(self, token_id):
        return self.bid_cache.get(token_id)


@pytest.fixture
def mock_engine():
    config = MagicMock()
    config.paper_trade = True
    config.bot_version = "v3.121"
    config.reversal_toggling_enabled = False

    validator = MagicMock()
    pos_mgr = MagicMock()
    pos_mgr.ws = DummyWS()
    pos_mgr.open_position = AsyncMock(return_value=True)

    clob = MagicMock()
    clob.get_orderbook = AsyncMock()
    pos_mgr.clob = clob

    db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone = AsyncMock(return_value=[0.0])
    mock_cursor.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=mock_cursor)

    rejection_logger = MagicMock()
    rejection_logger.log_rejection = AsyncMock()

    engine = DecisionEngine(
        config=config,
        validator=validator,
        position_mgr=pos_mgr,
        clob_client=clob,
        db=db,
        rejection_logger=rejection_logger,
    )
    return engine


@pytest.mark.asyncio
async def test_tier1_ws_cache_hit(mock_engine):
    """Tier 1: When WS cache has best ask, return immediately without calling REST."""
    mock_engine.position_mgr.ws.ask_cache["yes_token_123"] = 0.52
    
    price = await mock_engine._get_live_price_with_fallback("yes_token_123", counterpart_tid="no_token_123")
    
    assert price == 0.52
    mock_engine.clob.get_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_tier2_implied_counterpart_bid_hit(mock_engine):
    """Tier 2: When target token has no WS ask, derive implied ask from counterpart WS bid (1.0 - bid)."""
    # Target token has no ask
    assert "yes_token_123" not in mock_engine.position_mgr.ws.ask_cache
    # Counterpart token has a bid of 0.45 -> implied ask = 0.55
    mock_engine.position_mgr.ws.bid_cache["no_token_123"] = 0.45

    price = await mock_engine._get_live_price_with_fallback("yes_token_123", counterpart_tid="no_token_123")

    assert price == 0.55
    mock_engine.clob.get_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_tier3_rest_clob_orderbook_fallback(mock_engine):
    """Tier 3: When WS has no ask and no counterpart bid, query CLOB REST orderbook and prime WS cache."""
    # Both WS caches are empty
    mock_engine.clob.get_orderbook.return_value = {
        "asks": [{"price": "0.58", "size": "250.0"}]
    }

    price = await mock_engine._get_live_price_with_fallback("yes_token_123", counterpart_tid="no_token_123")

    assert price == 0.58
    mock_engine.clob.get_orderbook.assert_called_once_with("yes_token_123")
    # Verify WS cache is primed for subsequent zero-latency lookups
    assert mock_engine.position_mgr.ws.ask_cache["yes_token_123"] == 0.58


@pytest.mark.asyncio
async def test_tier4_all_tiers_empty_returns_none(mock_engine):
    """Tier 4: If all tiers fail, return None. Under NO circumstances return 0.50."""
    # Both WS caches are empty, and REST returns empty asks
    mock_engine.clob.get_orderbook.return_value = {"asks": []}

    price = await mock_engine._get_live_price_with_fallback("yes_token_123", counterpart_tid="no_token_123")

    assert price is None
    assert price != 0.50


@pytest.mark.asyncio
async def test_process_signal_rejection_on_insufficient_liquidity(mock_engine):
    """Verify that process_signal rejects signal with INSUFFICIENT_LIQUIDITY when prices cannot be resolved."""
    mock_engine.clob.get_orderbook.return_value = None

    market = {
        "market_id": "m_test_999",
        "question": "Will BTC go Up?",
        "slug": "btc-updown-5m-12345",
        "yes_token_id": "yes_tok_999",
        "no_token_id": "no_tok_999",
        "closes_at": 1787000000,
        "streak_depth": 2,
    }

    success, reason = await mock_engine.process_signal(market, side="YES")

    assert success is False
    assert "Orderbook liquidity unavailable" in reason
    mock_engine.rejection_logger.log_rejection.assert_called_once()
    call_args = mock_engine.rejection_logger.log_rejection.call_args[1]
    assert call_args["primary_reason"] == "INSUFFICIENT_LIQUIDITY"
