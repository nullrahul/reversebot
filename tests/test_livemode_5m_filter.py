import os
import asyncio
import pytest
import pytest_asyncio
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market
from src.strategy.profiles import ProfileManager


@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _sample_market(market_id: str = "0xtest_btc", **overrides) -> dict:
    base = {
        "market_id": market_id,
        "question": "Will BTC go UP?",
        "category": "Crypto",
        "yes_price": 0.06,
        "no_price": 0.94,
        "yes_volume_24h": 1500.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "closes_at": int(time.time()) + 900,
        "is_flagged": 0,
        "flag_reason": None,
        "last_seen_above_threshold": None,
        "consecutive_cycles_above": 0,
        "yes_token_id": f"yes_token_{market_id}",
        "no_token_id": f"no_token_{market_id}",
        "slug": f"btc-updown-15m-{market_id}",
        "window_minutes": 15
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_livemode_skips_15m_market_while_cmh_fires(db):
    """
    Test that for a 15-minute market, cross_market_hedge triggers its 2 virtual positions,
    but livemode is strictly SKIPPED (0 virtual trades, 0 live orders).
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr._paper = False
    mock_pos_mgr.config = MagicMock(live_profile_only="livemode")
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    mock_pos_mgr.open_position = AsyncMock(return_value=True)

    mock_ws = MagicMock()
    # Twin market ETH asks: YES = 0.945, NO = 0.055
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.945" if "yes" in tid else "0.055")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 900

    # 15-minute markets
    btc_market = _sample_market(
        "btc_15m",
        slug="btc-updown-15m-1788500000",
        question="Bitcoin Up or Down - 15-minute",
        window_minutes=15,
        closes_at=closes_at
    )
    eth_market = _sample_market(
        "eth_15m",
        slug="eth-updown-15m-1788500000",
        question="Ethereum Up or Down - 15-minute",
        window_minutes=15,
        closes_at=closes_at,
        yes_token_id="yes_token_eth_15m",
        no_token_id="no_token_eth_15m"
    )

    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # BTC YES @ 0.06, ETH NO @ 0.055 -> Both are < 0.07!
    await pm.evaluate_and_trigger(
        market=btc_market,
        side="YES",
        entry_price=0.06,
        size_usdc=10.0,
        shares=166.0,
        strategy_mode="TREND",
        elapsed_secs=10.0,
        persistence_score=0.5,
        should_block_for_loss=False,
        active_strategy="TREND",
        target_token_id="yes_token_btc_15m",
        streak_depth=2
    )

    # Allow any background tasks to run
    await asyncio.sleep(0.05)

    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    # CMH MUST trigger both 15m legs in dry run
    assert len(cmh_calls) == 2, f"Expected 2 CMH calls, got {len(cmh_calls)}"

    # livemode MUST be 0 (skipped for 15m)
    assert len(livemode_calls) == 0, f"Expected 0 livemode calls for 15m, got {len(livemode_calls)}"

    # Real open_position MUST NOT be called
    assert mock_pos_mgr.open_position.call_count == 0, "open_position should not be called for 15m market"


@pytest.mark.asyncio
async def test_livemode_triggers_on_5m_market(db):
    """
    Test that for a 5-minute market, livemode triggers BOTH virtual position
    and live real-money open_position call.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr._paper = False
    mock_pos_mgr.config = MagicMock(live_profile_only="livemode")
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    mock_pos_mgr.open_position = AsyncMock(return_value=True)

    mock_ws = MagicMock()
    # Twin market ETH asks: YES = 0.945, NO = 0.055
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.945" if "yes" in tid else "0.055")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300

    # 5-minute markets
    btc_market = _sample_market(
        "btc_5m",
        slug="btc-updown-5m-1788500000",
        question="Bitcoin Up or Down - 5-minute",
        window_minutes=5,
        closes_at=closes_at
    )
    eth_market = _sample_market(
        "eth_5m",
        slug="eth-updown-5m-1788500000",
        question="Ethereum Up or Down - 5-minute",
        window_minutes=5,
        closes_at=closes_at,
        yes_token_id="yes_token_eth_5m",
        no_token_id="no_token_eth_5m"
    )

    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # BTC YES @ 0.06, ETH NO @ 0.055 -> Both are < 0.07!
    await pm.evaluate_and_trigger(
        market=btc_market,
        side="YES",
        entry_price=0.06,
        size_usdc=10.0,
        shares=166.0,
        strategy_mode="TREND",
        elapsed_secs=10.0,
        persistence_score=0.5,
        should_block_for_loss=False,
        active_strategy="TREND",
        target_token_id="yes_token_btc_5m",
        streak_depth=2
    )

    # Allow any background tasks to run
    await asyncio.sleep(0.05)

    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    # Both CMH and livemode should trigger
    assert len(cmh_calls) == 2
    assert len(livemode_calls) == 2, f"Expected 2 livemode calls for 5m, got {len(livemode_calls)}"

    # Real open_position MUST be called for both 5m legs
    assert mock_pos_mgr.open_position.call_count == 2, f"Expected 2 open_position calls, got {mock_pos_mgr.open_position.call_count}"
