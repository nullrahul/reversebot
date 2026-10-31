import os
import asyncio
import pytest
import pytest_asyncio
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market
from src.strategy.profiles import ProfileManager
from src.strategy.position_mgr import PositionManager


@pytest_asyncio.fixture
async def db():
    """Create a temporary SQLite database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _sample_market(market_id: str = "0xtest_btc", **overrides) -> dict:
    base = {
        "market_id": market_id,
        "question": "Will BTC go UP in 5m?",
        "category": "Crypto",
        "yes_price": 0.06,
        "no_price": 0.94,
        "yes_volume_24h": 1500.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "closes_at": int(time.time()) + 300,
        "is_flagged": 0,
        "flag_reason": None,
        "last_seen_above_threshold": None,
        "consecutive_cycles_above": 0,
        "yes_token_id": f"yes_token_{market_id}",
        "no_token_id": f"no_token_{market_id}",
        "slug": f"btc-updown-5m-{market_id}"
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_livemode_mimic_single_sub_007_leg(db):
    """
    Test that when cross_market_hedge fires with one leg < 0.07 (0.06)
    and one leg >= 0.07 (0.35), livemode ONLY takes the 0.06 leg.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    
    mock_ws = MagicMock()
    # Twin market ETH asks: YES = 0.65, NO = 0.35
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.65" if "yes" in tid else "0.35")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300

    # Insert BTC market and ETH market with matching closes_at
    btc_market = _sample_market("btc_mkt_1", slug="btc-5m-updown", question="Will BTC go UP?", closes_at=closes_at)
    eth_market = _sample_market("eth_mkt_1", slug="eth-5m-updown", question="Will ETH go UP?", closes_at=closes_at,
                               yes_token_id="yes_token_eth_mkt_1", no_token_id="no_token_eth_mkt_1")
    
    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # Trigger signal evaluation for BTC market
    # cur_yes_p = 0.06 (< 0.40), twin_no_p = 0.35 (< 0.40) -> trigger_case_1 True!
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
        target_token_id="yes_token_btc_mkt_1",
        streak_depth=2
    )

    # Inspect open_profile_position calls
    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    # CMH should take both legs (BTC YES @ 0.06 and ETH NO @ 0.35)
    assert len(cmh_calls) == 2, f"Expected 2 CMH calls, got {len(cmh_calls)}"
    
    # livemode should take ONLY the 0.06 leg (BTC)
    assert len(livemode_calls) == 1, f"Expected 1 livemode call, got {len(livemode_calls)}"
    assert livemode_calls[0].kwargs.get("market_id") == "btc_mkt_1"
    assert livemode_calls[0].kwargs.get("entry_price") == 0.06
    assert livemode_calls[0].kwargs.get("side") == "YES"


@pytest.mark.asyncio
async def test_livemode_mimic_both_sub_007_legs(db):
    """
    Test that when cross_market_hedge fires with BOTH legs < 0.07 (e.g. 0.06 and 0.055),
    livemode takes both legs.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    
    mock_ws = MagicMock()
    # Twin market ETH asks: YES = 0.945, NO = 0.055
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.945" if "yes" in tid else "0.055")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300

    btc_market = _sample_market("btc_mkt_2", slug="btc-5m-updown", question="Will BTC go UP?", closes_at=closes_at)
    eth_market = _sample_market("eth_mkt_2", slug="eth-5m-updown", question="Will ETH go UP?", closes_at=closes_at,
                               yes_token_id="yes_token_eth_mkt_2", no_token_id="no_token_eth_mkt_2")
    
    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # BTC YES @ 0.06, ETH NO @ 0.055 -> trigger_case_1 True, both < 0.07
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
        target_token_id="yes_token_btc_mkt_2",
        streak_depth=2
    )

    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    assert len(cmh_calls) == 2
    assert len(livemode_calls) == 2
    market_ids = {c.kwargs.get("market_id") for c in livemode_calls}
    assert market_ids == {"btc_mkt_2", "eth_mkt_2"}


@pytest.mark.asyncio
async def test_livemode_zero_trades_when_both_ge_007(db):
    """
    Test that when cross_market_hedge fires with both legs >= 0.07 (e.g. 0.20 and 0.30),
    livemode does NOT take any trade.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    
    mock_ws = MagicMock()
    # Twin market ETH asks: YES = 0.70, NO = 0.30
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.70" if "yes" in tid else "0.30")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300

    btc_market = _sample_market("btc_mkt_3", slug="btc-5m-updown", question="Will BTC go UP?", closes_at=closes_at)
    eth_market = _sample_market("eth_mkt_3", slug="eth-5m-updown", question="Will ETH go UP?", closes_at=closes_at,
                               yes_token_id="yes_token_eth_mkt_3", no_token_id="no_token_eth_mkt_3")
    
    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # BTC YES @ 0.20, ETH NO @ 0.30 -> trigger_case_1 True, but both >= 0.07
    await pm.evaluate_and_trigger(
        market=btc_market,
        side="YES",
        entry_price=0.20,
        size_usdc=10.0,
        shares=50.0,
        strategy_mode="TREND",
        elapsed_secs=10.0,
        persistence_score=0.5,
        should_block_for_loss=False,
        active_strategy="TREND",
        target_token_id="yes_token_btc_mkt_3",
        streak_depth=2
    )

    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    assert len(cmh_calls) == 2
    assert len(livemode_calls) == 0, "livemode should not take any trades when prices >= 0.07"


@pytest.mark.asyncio
async def test_livemode_never_fires_if_cmh_skipped(db):
    """
    Test that if CMH skips (e.g. cur_price < 0.05 price floor), livemode NEVER fires.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    
    mock_ws = MagicMock()
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.70" if "yes" in tid else "0.30")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300

    btc_market = _sample_market("btc_mkt_4", slug="btc-5m-updown", question="Will BTC go UP?", closes_at=closes_at)
    eth_market = _sample_market("eth_mkt_4", slug="eth-5m-updown", question="Will ETH go UP?", closes_at=closes_at,
                               yes_token_id="yes_token_eth_mkt_4", no_token_id="no_token_eth_mkt_4")
    
    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

    # BTC YES @ 0.04 (< 0.05 floor) -> CMH skips due to floor
    await pm.evaluate_and_trigger(
        market=btc_market,
        side="YES",
        entry_price=0.04,
        size_usdc=10.0,
        shares=250.0,
        strategy_mode="TREND",
        elapsed_secs=10.0,
        persistence_score=0.5,
        should_block_for_loss=False,
        active_strategy="TREND",
        target_token_id="yes_token_btc_mkt_4",
        streak_depth=2
    )

    calls = mock_pos_mgr.open_profile_position.call_args_list
    cmh_calls = [c for c in calls if c.kwargs.get("profile_name") == "cross_market_hedge"]
    livemode_calls = [c for c in calls if c.kwargs.get("profile_name") == "livemode"]

    assert len(cmh_calls) == 0
    assert len(livemode_calls) == 0


@pytest.mark.asyncio
async def test_livemode_excluded_from_stop_loss_in_position_mgr():
    """
    Test that livemode is NOT in profiles_with_sl or profiles_with_tp.
    """
    from src.strategy.position_mgr import PositionManager
    import inspect

    # Read the monitor_positions source code to verify profiles_with_sl and profiles_with_tp
    source = inspect.getsource(PositionManager.monitor_positions)
    assert "'livemode'" not in source, "livemode should never be in profiles_with_sl or profiles_with_tp"


@pytest.mark.asyncio
async def test_livemode_live_order_execution_when_live_mode(db):
    """
    Test that when PositionManager is in LIVE mode (_paper=False) and live_profile_only="livemode",
    livemode triggers a real open_position call with size_usdc=1.0.
    """
    mock_pos_mgr = AsyncMock()
    mock_pos_mgr._paper = False
    mock_pos_mgr.config = MagicMock(live_profile_only="livemode")
    mock_pos_mgr.open_profile_position = AsyncMock(return_value=True)
    mock_pos_mgr.open_position = AsyncMock(return_value=True)

    mock_ws = MagicMock()
    mock_ws.get_best_ask = MagicMock(side_effect=lambda tid: "0.65" if "yes" in tid else "0.35")
    mock_pos_mgr.ws = mock_ws

    closes_at = int(time.time()) + 300
    btc_market = _sample_market("btc_mkt_live", slug="btc-5m-updown", question="Will BTC go UP?", closes_at=closes_at)
    eth_market = _sample_market("eth_mkt_live", slug="eth-5m-updown", question="Will ETH go UP?", closes_at=closes_at,
                               yes_token_id="yes_token_eth_mkt_live", no_token_id="no_token_eth_mkt_live")
    
    await upsert_market(db, btc_market)
    await upsert_market(db, eth_market)

    pm = ProfileManager(db, mock_pos_mgr)
    await pm._init_opened_list()

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
        target_token_id="yes_token_btc_mkt_live",
        streak_depth=2
    )

    # Allow spawned asyncio tasks to run
    await asyncio.sleep(0.05)

    # Verify open_position was called for the 0.06 leg
    assert mock_pos_mgr.open_position.called, "open_position should be called for livemode in live mode"
    live_call = mock_pos_mgr.open_position.call_args.kwargs
    assert live_call.get("market_id") == "btc_mkt_live"
    assert live_call.get("entry_price") == 0.06
    assert live_call.get("size_usdc") == 1.0
    assert live_call.get("strategy_mode") == "LIVEMODE_CMH"


@pytest.mark.asyncio
async def test_decision_engine_bypasses_core_momentum_when_live_profile_only(db):
    """
    Test that when DecisionEngine runs in live mode (_paper=False) and live_profile_only="livemode",
    core momentum does NOT call open_position.
    """
    from src.strategy.decision import DecisionEngine
    from src.strategy.validator import SignalValidator
    
    cfg = MagicMock()
    cfg.bot_version = "test_v1"
    cfg.paper_trade = False
    cfg.position_size_usdc = 10.0
    cfg.min_ev_threshold = 0.01
    cfg.max_total_exposure_usdc = 100.0
    cfg.live_profile_only = "livemode"
    cfg.short_window_min_seconds_to_close = 60
    cfg.min_seconds_to_close_pre_entry = 60
    cfg.timeframe_15m_enabled = True
    cfg.lossmomentum_skip_enabled = False
    cfg.regime_filter_enabled = False
    cfg.max_no_ask_multiplier = 1.05
    cfg.high_vol_ask_multiplier = 1.10
    cfg.high_vol_assets = ["BTC", "ETH"]

    mock_pm = AsyncMock()
    mock_pm._paper = False
    mock_pm.open_position = AsyncMock(return_value=True)
    mock_pm.ws = MagicMock()
    mock_pm.ws.is_connected = True
    mock_pm.ws.bid_cache = {"yes_token_test": 0.49, "no_token_test": 0.49}
    mock_pm.ws.ask_cache = {"yes_token_test": 0.51, "no_token_test": 0.51}
    mock_pm.ws.get_best_ask = MagicMock(return_value=0.51)

    mock_validator = AsyncMock()
    mock_val_res = MagicMock()
    mock_val_res.passed = True
    mock_val_res.checks = {}
    mock_val_res.reasons = []
    mock_validator.validate = AsyncMock(return_value=mock_val_res)

    mock_clob = AsyncMock()
    mock_clob.get_synced_time = MagicMock(return_value=int(time.time()))

    decision = DecisionEngine(
        config=cfg,
        validator=mock_validator,
        position_mgr=mock_pm,
        clob_client=mock_clob,
        db=db
    )

    market = _sample_market("test_core_bypass", yes_price=0.51, no_price=0.49, closes_at=int(time.time()) + 200)
    await upsert_market(db, market)

    success, reason = await decision.process_signal(market, target_token_id="yes_token_test", side="YES")
    
    assert success is True
    assert "live orders restricted to livemode" in reason.lower()
    assert not mock_pm.open_position.called, "Core momentum should NOT call open_position in live mode"

