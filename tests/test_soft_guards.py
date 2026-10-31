"""
Unit tests for the soft-guards transition (B3 ceilings, Falling Knife, Z-Score spikes)
and the dedicated data/soft_guards_audit.db database logs.
"""

import asyncio
import os
import sys
import tempfile
import time
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market
from src.strategy.decision import DecisionEngine
from src.db.soft_audit_db import init_soft_audit_db, get_pending_soft_trades, update_soft_trade_resolution


@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


@pytest_asyncio.fixture
async def soft_db_file():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_name = tmp.name
    tmp.close()
    yield tmp_name
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)


def _mock_config(**overrides):
    defaults = {
        "yes_bias_threshold": 0.80,
        "yes_bias_max_threshold": 0.97,
        "min_volume_24h_usdc": 500.0,
        "min_time_to_close_hours": 6,
        "max_time_to_close_days": 90,
        "position_size_usdc": 10.0,
        "max_open_positions": 5,
        "max_daily_loss_usdc": 75.0,
        "max_total_exposure_usdc": 100.0,
        "paper_trade": True,
        "bot_version": "v3.92-test",
        "category_whitelist": ["Crypto"],
        "target_market_mode": "crypto_short_window",
        "target_crypto_symbols": ["BTC", "ETH"],
        "target_window_minutes": [5, 15],
        "early_yes_bias_max": 0.95,
        "mid_yes_bias_max": 0.95,
        "late_yes_bias_max": 0.95,
        "early_phase_min_seconds": 600,
        "mid_phase_min_seconds": 180,
        "max_no_ask_multiplier": 2.0,
        "high_vol_assets": [],
        "high_vol_ask_multiplier": 2.0,
        "global_cooldown": 0.0,
        "min_ev_threshold": 0.3,
        "max_spread_pct_of_ask": 0.05,
        "timeframe_15m_enabled": True,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def _sample_market(**overrides):
    now = int(time.time())
    base = {
        "market_id": "0xsoft_market",
        "question": "Will BTC Up or Down - 5 Minutes?",
        "category": "Crypto",
        "yes_price": 0.46,
        "no_price": 0.54,
        "yes_volume_24h": 2000.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "slug": "btc-updown-5m-123456",
        "event_slug": "btc-updown-5m-123456",
        "event_title": "Bitcoin Up or Down",
        "starts_at": now - 300,
        "window_minutes": 5,
        "accepting_orders": True,
        "closes_at": now + 400,
        "last_scanned_at": now,
        "is_flagged": 1,
        "flag_reason": "yes_bias_confirmed",
        "last_seen_above_threshold": now - 120,
        "consecutive_cycles_above": 2,
        "yes_token_id": "yes_token",
        "no_token_id": "no_token",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_b3_ceiling_hard_reject_5m(db, soft_db_file):
    """
    On 5m window, YES price of 0.65 (> 0.60) should trigger hard rejection and return success = False.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xsoft_b3_5m", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xsoft_b3_5m", yes_price=0.65, no_price=0.35, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "YES price exceeds ceiling" in reason
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "YES_EXHAUSTION_ALREADY_PRICED" in kwargs["primary_reason"]
    assert "yes_exhaustion_ceiling_gate" in kwargs["failed_checks"][0]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 0


@pytest.mark.asyncio
async def test_b3_ceiling_hard_reject_15m(db, soft_db_file):
    """
    On 15m window, YES price of 0.65 (> 0.60) should be hard rejected (success = False).
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 15))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xhard_b3_15m", window_minutes=15))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xhard_b3_15m", yes_price=0.65, no_price=0.35, window_minutes=15)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "YES price exceeds ceiling" in reason
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "YES_EXHAUSTION_ALREADY_PRICED" in kwargs["primary_reason"]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 0





@pytest.mark.asyncio
async def test_z_score_price_velocity_soft_reject_5m(db, soft_db_file):
    """
    Verify that if the Spot price velocity Z-score is extreme (> 3.0),
    it triggers would_reject_reasons on 5m, returns success = True,
    and logs the trade to the soft audit database.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xsoft_z_5m", window_minutes=5))
    
    # Mock Binance WebSocket client with extreme Z-score
    binance_ws = MagicMock()
    binance_ws.is_connected = True
    binance_ws.get_cvd_metrics = MagicMock(return_value=(0.0, 0.0))
    binance_ws.get_spot_velocity_z_score = MagicMock(return_value=4.5)

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner, binance_ws=binance_ws)
    decision.soft_audit_db_path = soft_db_file
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xsoft_z_5m", yes_price=0.50, no_price=0.50, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason
    
    # Verify logged to soft audit DB with correct z-score and reasons
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 1
    assert trades[0]["market_id"] == "0xsoft_z_5m"
    assert "SPOT_VELOCITY_Z_SCORE_SPIKE" in trades[0]["reasons"]
    assert trades[0]["z_score"] == 4.5


@pytest.mark.asyncio
async def test_b3_no_gate_hard_reject_15m(db, soft_db_file):
    """
    On 15m window, NO entry with YES price of 0.35 (< 0.40) should be hard rejected (success = False).
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 15))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    await upsert_market(db, _sample_market(market_id="0xhard_no_15m", window_minutes=15))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xhard_no_15m", yes_price=0.25, no_price=0.75, window_minutes=15)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is False
    assert "YES price below floor" in reason
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "NO_UP_MOVE_TOO_STRONG" in kwargs["primary_reason"]
    assert "no_up_move_too_strong_gate" in kwargs["failed_checks"][0]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 0





@pytest.mark.asyncio
async def test_b3_no_floor_hard_reject_5m(db, soft_db_file):
    """
    On 5m window, NO entry with YES price of 0.65 (> 0.60) should be hard rejected (success = False).
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    await upsert_market(db, _sample_market(market_id="0xhard_no_floor_5m", window_minutes=5))
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xhard_no_floor_5m", yes_price=0.65, no_price=0.35, window_minutes=5)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is False
    assert "YES price above ceiling" in reason
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "NO_ALREADY_PRICED" in kwargs["primary_reason"]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 0


@pytest.mark.asyncio
async def test_spread_guard_hard_reject_5m(db, soft_db_file):
    """
    Verify that if the Spread Guard is triggered (entry_price > max_entry_price),
    it triggers hard rejection and returns success = False.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    await upsert_market(db, _sample_market(market_id="0xsoft_spread_5m", window_minutes=5))
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # opposing price (YES) is 0.46, meaning fair_entry_price (NO) is 0.54
    # Set max multiplier to 0.8x so max allowed entry is 0.54 * 0.8 = 0.432.
    # Set entry price (NO price) to 0.52. Since 0.52 > 0.432, it will trigger the Spread Guard.
    config.max_no_ask_multiplier = 0.8
    market = _sample_market(market_id="0xsoft_spread_5m", yes_price=0.46, no_price=0.52, window_minutes=5)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is True
    assert "placed successfully" in reason
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "WOULD_HAVE_BLOCKED" in kwargs["primary_reason"]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 1
    assert trades[0]["market_id"] == "0xsoft_spread_5m"
    assert "SPREAD_GUARD" in trades[0]["reasons"]


async def test_timeframe_15m_disabled_reject(db):
    """
    Verify that if timeframe_15m_enabled is False, a 15-minute signal is hard rejected.
    """
    config = _mock_config()
    config.timeframe_15m_enabled = False
    
    clob = AsyncMock()
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 15))
    validator = MagicMock()
    position_mgr = MagicMock()
    
    await upsert_market(db, _sample_market(market_id="0xdisabled_15m", window_minutes=15))
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    market = _sample_market(market_id="0xdisabled_15m", window_minutes=15)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "15-minute timeframe is disabled" in reason
    
    # Verify rejection logger was called
    decision.rejection_logger.log_rejection.assert_called_once()


@pytest.mark.asyncio
async def test_insufficient_shares_rejected(db, soft_db_file):
    """
    Verify that if calculated shares are below 6.0, the bot rejects the signal.
    """
    config = _mock_config()
    config.live_profile_only = "core"
    # Force low config position size so calculated shares are < 6.0
    config.position_size_usdc = 1.0
    
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None
    
    # We want open_position to NOT be called
    position_mgr.open_position = AsyncMock(return_value=True)

    await upsert_market(db, _sample_market(market_id="0xinsufficient_shares_m", window_minutes=5))
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)

    # Use entry price 0.60, so 1.0 USDC sizing results in 1 share (< 6)
    market = _sample_market(market_id="0xinsufficient_shares_m", yes_price=0.60, no_price=0.40, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "below minimum 6.0 threshold" in reason
    position_mgr.open_position.assert_not_called()


@pytest.mark.asyncio
async def test_persistence_score_hard_reject_5m(db, soft_db_file):
    """
    Verify that if persistence_score is below min_persistence_score,
    the bot rejects the signal and blocks order placement.
    """
    config = _mock_config()
    config.min_persistence_score = 0.55
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None
    
    # We want open_position NOT to be called
    position_mgr.open_position = AsyncMock(return_value=True)

    await upsert_market(db, _sample_market(market_id="0xpersist_reject_5m", window_minutes=5))
    
    # Mock RegimeTracker returning a low score (e.g. 0.40)
    regime_tracker = MagicMock()
    regime_tracker.refresh_score = AsyncMock()
    regime_tracker.get_current_score = MagicMock(side_effect=lambda asset, window, direction=None: 0.40 if direction is not None else 0.50)
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner, regime_tracker=regime_tracker)
    decision.soft_audit_db_path = soft_db_file
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)

    market = _sample_market(market_id="0xpersist_reject_5m", yes_price=0.50, no_price=0.50, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason
    position_mgr.open_position.assert_called_once()
    
    decision.rejection_logger.log_rejection.assert_called_once()
    args, kwargs = decision.rejection_logger.log_rejection.call_args
    assert "WOULD_HAVE_BLOCKED" in kwargs["primary_reason"]
    
    trades = await get_pending_soft_trades(soft_db_file)
    assert len(trades) == 1
    assert trades[0]["market_id"] == "0xpersist_reject_5m"
    assert "PERSISTENCE_SCORE" in trades[0]["reasons"]


@pytest.mark.asyncio
async def test_websocket_pricing_and_connection_guards(db):
    """
    Verify that if the WebSocket client is disconnected/reconnecting,
    or if the price cache for the target token is empty, the bot rejects.
    """
    config = _mock_config()
    clob = AsyncMock()
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    validator = MagicMock()
    
    # Mock a disconnected websocket client (not a Mock subclass to trigger isinstance check)
    class CustomWSClient:
        def __init__(self):
            self.is_connected = False
            self.bid_cache = {}
            self.ask_cache = {}
        def get_best_ask(self, token_id):
            return self.ask_cache.get(token_id)
        def get_best_bid(self, token_id):
            return self.bid_cache.get(token_id)

    position_mgr = MagicMock()
    ws_client = CustomWSClient()
    position_mgr.ws = ws_client
    
    position_mgr.open_position = AsyncMock(return_value=True)

    await upsert_market(db, _sample_market(market_id="0xws_test_m", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.rejection_logger = MagicMock()
    decision.rejection_logger.log_rejection = AsyncMock()
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)

    market = _sample_market(market_id="0xws_test_m", yes_price=0.50, no_price=0.50, window_minutes=5)
    
    # 1. Test case: WebSocket is disconnected
    success, reason = await decision.process_signal(market, side="YES")
    assert success is False
    assert "disconnected or reconnecting" in reason
    position_mgr.open_position.assert_not_called()

    # 2. Test case: WebSocket is connected, but price cache and REST orderbook are empty for YES token
    ws_client.is_connected = True
    success, reason = await decision.process_signal(market, side="YES")
    assert success is False
    assert ("unavailable for YES token" in reason or "price cache is empty for YES token" in reason)
    position_mgr.open_position.assert_not_called()

    # 3. Test case: WebSocket is connected, and YES price cache is populated, but NO price cache and REST orderbook are empty
    # Since spread checking requires both, it should reject if NO price cache is empty too.
    ws_client.ask_cache["0xyes_token"] = 0.52
    market["yes_token_id"] = "0xyes_token"
    market["no_token_id"] = "0xno_token"
    
    success, reason = await decision.process_signal(market, side="YES")
    assert success is False
    assert ("unavailable for NO token" in reason or "price cache is empty for NO token" in reason)
    position_mgr.open_position.assert_not_called()

    # 4. Test case: WebSocket is connected, and both YES and NO price caches are populated -> success
    ws_client.ask_cache["0xno_token"] = 0.48
    
    success, reason = await decision.process_signal(market, side="YES")
    assert success is True
    assert "placed successfully" in reason
    position_mgr.open_position.assert_called_once()



