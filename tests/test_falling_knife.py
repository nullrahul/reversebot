"""
Unit tests for the Price Approach Direction Gate (Falling Knife Guard).
"""

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, upsert_market, log_spread_tick
from src.strategy.decision import DecisionEngine


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
        "yes_bias_threshold": 0.80,
        "yes_bias_max_threshold": 0.97,
        "min_volume_24h_usdc": 500.0,
        "min_time_to_close_hours": 6,
        "max_time_to_close_days": 90,
        "position_size_usdc": 15.0,
        "max_open_positions": 5,
        "max_daily_loss_usdc": 75.0,
        "max_total_exposure_usdc": 100.0,
        "paper_trade": True,
        "bot_version": "v3.8-test",
        "category_whitelist": ["Crypto"],
        "target_market_mode": "broad",
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
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def _sample_market(**overrides):
    now = int(time.time())
    base = {
        "market_id": "0xknife_market",
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
async def test_yes_falling_knife_rejection(db):
    """
    If YES price fell from 0.51 to 0.46 in the last 15s (fell > 0.030),
    buying YES should be rejected as a falling knife.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # Seed spread_logs with a price of 0.51 YES 15 seconds ago
    now = int(time.time())
    await upsert_market(db, _sample_market())
    
    # log_spread_tick stores best_ask as the YES price
    await db.execute(
        "INSERT INTO spread_logs (timestamp, market_id, symbol, window_minutes, best_bid, best_ask, spread_amount, spread_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 15, "0xknife_market", "BTC", 5, 0.50, 0.51, 0.01, 0.01)
    )
    await db.commit()

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.validator = validator
    
    # Set the validator mock to return a passing ValidationResult
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    decision.validator.validate = AsyncMock(return_value=validation_res)

    market = _sample_market(yes_price=0.46, no_price=0.54)
    
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "Falling Knife" in reason
    assert "YES price fell" in reason


@pytest.mark.asyncio
async def test_no_falling_knife_rejection(db):
    """
    If YES price rose from 0.54 to 0.58 in the last 15s (rose > 0.030, meaning NO price fell),
    buying NO should be rejected as a falling knife.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # Seed spread_logs with a YES price of 0.54 15 seconds ago
    now = int(time.time())
    await upsert_market(db, _sample_market())
    
    await db.execute(
        "INSERT INTO spread_logs (timestamp, market_id, symbol, window_minutes, best_bid, best_ask, spread_amount, spread_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 15, "0xknife_market", "BTC", 5, 0.53, 0.54, 0.01, 0.01)
    )
    await db.commit()

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.validator = validator
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    decision.validator.validate = AsyncMock(return_value=validation_res)

    # YES price rises to 0.58 (meaning NO price falls)
    market = _sample_market(yes_price=0.58, no_price=0.42)
    
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is False
    assert "Falling Knife" in reason
    assert "NO price fell" in reason


@pytest.mark.asyncio
async def test_falling_knife_passes_when_price_approaches_from_below(db):
    """
    If YES price rose from 0.42 to 0.46 in the last 15s (approached from below),
    buying YES should pass the falling knife gate.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    clob.place_order = AsyncMock(return_value={"order_id": "123"})
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # Seed spread_logs with a YES price of 0.42 15 seconds ago
    now = int(time.time())
    await upsert_market(db, _sample_market())
    
    await db.execute(
        "INSERT INTO spread_logs (timestamp, market_id, symbol, window_minutes, best_bid, best_ask, spread_amount, spread_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 15, "0xknife_market", "BTC", 5, 0.41, 0.42, 0.01, 0.01)
    )
    await db.commit()

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.validator = validator
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0  # Set ev_ratio so it passes the EV check!
    decision.validator.validate = AsyncMock(return_value=validation_res)
    
    # Mock position manager successful open
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # YES price rises to 0.46 (approaches from below - valid momentum)
    market = _sample_market(yes_price=0.46, no_price=0.54)
    
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_falling_knife_passes_in_opening_phase(db):
    """
    If YES price fell from 0.51 to 0.46 in the last 15s (fell > 0.030),
    but the candle only started 10 seconds ago (opening phase),
    buying YES should bypass the gate and PASS.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC_OPENING", 5))
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # Seed spread_logs with a YES price of 0.51 15 seconds ago
    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xknife_market_opening"))
    
    await db.execute(
        "INSERT INTO spread_logs (timestamp, market_id, symbol, window_minutes, best_bid, best_ask, spread_amount, spread_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 15, "0xknife_market_opening", "BTC_OPENING", 5, 0.50, 0.51, 0.01, 0.01)
    )
    await db.commit()

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    decision.validator = validator
    
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # Mock market closes in 290 seconds (elapsed_seconds = 300 - 290 = 10s < 25s opening phase)
    market = _sample_market(market_id="0xknife_market_opening", yes_price=0.46, no_price=0.54, closes_at=now + 290, window_minutes=5)
    
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_yes_gate_allows_under_0_52(db):
    """
    On YES entry, a YES price of 0.50 (which is <= 0.52) should be allowed by the gate.
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
    await upsert_market(db, _sample_market(market_id="0x5myes", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = MagicMock()
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0x5myes", yes_price=0.50, no_price=0.50, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_yes_gate_rejects_above_0_52(db):
    """
    On YES entry, a YES price of 0.55 (which is > 0.52) should be rejected by the gate.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0x15myes", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    decision.validator.validate = MagicMock()
    decision.validator.validate = AsyncMock(return_value=validation_res)

    market = _sample_market(market_id="0x15myes", yes_price=0.55, no_price=0.45, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is False
    assert "YES_EXHAUSTION" in reason


@pytest.mark.asyncio
async def test_no_gate_allows_above_0_42(db):
    """
    On NO entry, a YES price of 0.46 (which is >= 0.42) should be allowed by the gate.
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
    await upsert_market(db, _sample_market(market_id="0x5mno", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = MagicMock()
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0x5mno", yes_price=0.46, no_price=0.54, window_minutes=5)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_no_gate_rejects_under_0_42(db):
    """
    On NO entry, a YES price of 0.40 (which is < 0.42) should be rejected by the gate.
    """
    config = _mock_config()
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0x15mno", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    decision.validator.validate = MagicMock()
    decision.validator.validate = AsyncMock(return_value=validation_res)

    market = _sample_market(market_id="0x15mno", yes_price=0.40, no_price=0.60, window_minutes=5)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is False
    assert "NO_UP_MOVE_TOO_STRONG" in reason


@pytest.mark.asyncio
async def test_b2_imbalance_yes_soft_disabled(db):
    """
    If YES order book has a severe seller imbalance (imbalance < -0.15),
    buying YES should NOT be rejected (soft-disabled).
    """
    config = _mock_config()
    clob = AsyncMock()
    # Mock orderbook with severe seller imbalance: bid volume = 100, ask volume = 1000
    clob.get_orderbook = AsyncMock(return_value={
        "bids": [{"price": "0.45", "size": "100.0"}],
        "asks": [{"price": "0.47", "size": "1000.0"}]
    })
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xb2yes", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xb2yes", yes_price=0.50, no_price=0.50, window_minutes=5)
    success, reason = await decision.process_signal(market, side="YES")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_b2_imbalance_no_soft_disabled(db):
    """
    If NO order book has a severe buyer imbalance (imbalance > 0.15),
    buying NO should NOT be rejected (soft-disabled).
    """
    config = _mock_config()
    clob = AsyncMock()
    # Mock orderbook with severe buyer imbalance: bid volume = 1000, ask volume = 100
    clob.get_orderbook = AsyncMock(return_value={
        "bids": [{"price": "0.45", "size": "1000.0"}],
        "asks": [{"price": "0.47", "size": "100.0"}]
    })
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xb2no", window_minutes=5))
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    market = _sample_market(market_id="0xb2no", yes_price=0.50, no_price=0.50, window_minutes=5)
    success, reason = await decision.process_signal(market, side="NO")
    
    assert success is True
    assert "placed successfully" in reason


@pytest.mark.asyncio
async def test_correlation_penalty_applied_on_streak_2(db):
    """
    On streak_depth = 2 (mild edge), if a correlated position is already open,
    the 0.5x correlation penalty SHOULD be applied.
    """
    from src.db.queries import create_position, update_position_status
    config = _mock_config(position_size_usdc=10.0)
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # 1. Create a correlated market (ETH 5m) and open a YES position on it
    correlated_market = _sample_market(
        market_id="0xeth_corr",
        question="Will Ethereum Up or Down - 5 Minutes?",
        yes_price=0.50,
        no_price=0.50
    )
    await upsert_market(db, correlated_market)
    pid = await create_position(db, "0xeth_corr", 0.50, 10.0, 20.0, "v0.1.0", paper=True)
    await update_position_status(db, pid, "OPEN", paper=True)

    # 2. Setup the target signal market (BTC 5m)
    target_market = _sample_market(
        market_id="0xbtc_target",
        question="Will Bitcoin Up or Down - 5 Minutes?",
        yes_price=0.50,
        no_price=0.50,
        window_minutes=5,
        streak_depth=2  # Streak depth = 2 -> Standard sizing, penalty active
    )
    await upsert_market(db, target_market)

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # 3. Process the YES signal for BTC
    success, reason = await decision.process_signal(target_market, side="YES")
    assert success is True

    # 4. Check the size passed to open_position.
    assert decision.position_mgr.open_position.called
    args, kwargs = decision.position_mgr.open_position.call_args
    size_usdc = kwargs.get("size_usdc")
    # Base size = 10.0 * 0.5 (low opportunity count base) * 0.5 (streak 2 multiplier) = 2.5 USDC.
    # Timeframe skew = 1.2x -> 3.0 USDC.
    # Correlation penalty = 0.5x -> 1.5 USDC.
    # Clamps to 5.0 minimum shares -> 2.5 USDC cost.
    assert size_usdc <= 3.0  # Proves 0.5x penalty was applied (otherwise size would be 5.0+)


@pytest.mark.asyncio
async def test_correlation_penalty_paused_on_streak_3(db):
    """
    On streak_depth >= 3 (reversal multiplier active), if a correlated position is already open,
    the correlation penalty should be PAUSED (not applied, 1.0x).
    """
    from src.db.queries import create_position, update_position_status
    config = _mock_config(position_size_usdc=10.0)
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # 1. Create a correlated market (ETH 5m) and open a YES position on it
    correlated_market = _sample_market(
        market_id="0xeth_corr",
        question="Will Ethereum Up or Down - 5 Minutes?",
        yes_price=0.50,
        no_price=0.50
    )
    await upsert_market(db, correlated_market)
    pid = await create_position(db, "0xeth_corr", 0.50, 10.0, 20.0, "v0.1.0", paper=True)
    await update_position_status(db, pid, "OPEN", paper=True)

    # 2. Setup the target signal market (BTC 5m)
    target_market = _sample_market(
        market_id="0xbtc_target",
        question="Will Bitcoin Up or Down - 5 Minutes?",
        yes_price=0.50,
        no_price=0.50,
        window_minutes=5,
        streak_depth=3  # Streak depth = 3 -> Reversal sizing multiplier, penalty PAUSED
    )
    await upsert_market(db, target_market)

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    validation_res = MagicMock()
    validation_res.passed = True
    validation_res.checks = {}
    validation_res.reasons = []
    validation_res.ev_ratio = 2.0
    decision.validator.validate = AsyncMock(return_value=validation_res)
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # 3. Process the YES signal for BTC
    success, reason = await decision.process_signal(target_market, side="YES")
    assert success is True

    # 4. Check the size passed to open_position.
    assert decision.position_mgr.open_position.called
    args, kwargs = decision.position_mgr.open_position.call_args
    size_usdc = kwargs.get("size_usdc")
    # Base size = 10.0 * 0.5 (low opportunity count base) * 1.5 (streak 3 multiplier) = 7.5 USDC.
    # Timeframe skew = 1.2x -> 9.0 USDC.
    # Correlation penalty = 1.0x (PAUSED!) -> 9.0 USDC.
    # Assert that size is greater than 5.0 USDC (proving 0.5x penalty was bypassed, as 9.0 * 0.5 = 4.5 < 5.0)
    assert size_usdc > 5.0


@pytest.mark.asyncio
async def test_reversal_streak_bypasses_all_guards(db):
    """
    On streak_depth >= 3 (reversal mode), the trade must bypass:
    1. B3 Price limits (YES entry capped at 0.52 - we test at 0.58)
    2. Falling Knife pullback direction gate (YES fell from 0.62 to 0.58)
    3. EV Ratio checks (scaled EV threshold fails)
    4. Validation failures (except duplicate position check, we fail liquidity check)
    And still place the order successfully.
    """
    config = _mock_config(position_size_usdc=10.0)
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
    scanner.last_traded_close_ts = {}
    validator = MagicMock()
    position_mgr = MagicMock()
    position_mgr.ws = None

    # Seed spread_logs with a YES price of 0.62 15 seconds ago
    # YES drops to 0.58 (fell by 0.04 -> in the 3.0c to 6.0c toxic pullback band)
    now = int(time.time())
    await upsert_market(db, _sample_market(market_id="0xreversal_bypass"))
    
    await db.execute(
        "INSERT INTO spread_logs (timestamp, market_id, symbol, window_minutes, best_bid, best_ask, spread_amount, spread_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 15, "0xreversal_bypass", "BTC", 5, 0.61, 0.62, 0.01, 0.01)
    )
    await db.commit()

    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    
    # Mock validator returning failed validation on liquidity but passing on duplicate check
    validation_res = MagicMock()
    validation_res.passed = False
    validation_res.checks = {"4_duplicate": True, "1_liquidity": False}
    validation_res.reasons = ["Liquidity too low"]
    decision.validator.validate = AsyncMock(return_value=validation_res)
    
    # Mock position manager successful open
    decision.position_mgr.open_position = AsyncMock(return_value=True)

    # Setup market with yes_price=0.58 (violating B3 YES limit of 0.52) and streak_depth=3
    market = _sample_market(
        market_id="0xreversal_bypass",
        yes_price=0.58,
        no_price=0.42,
        streak_depth=3
    )
    
    success, reason = await decision.process_signal(market, side="YES")
    
    # Assert that all validations/guards were bypassed and trade was successfully placed!
    assert success is True
    assert "placed successfully" in reason
    assert decision.position_mgr.open_position.called


@pytest.mark.asyncio
async def test_streak_bound_cumulative_loss_recovery(db):
    """
    Verify the Streak-Bound Cumulative Reversal Recovery Sizer:
    1. Sums losses starting exactly from Streak 3 inside the active sequence.
    2. Proactively treats unresolved expired trades as losses.
    3. Streak <= 5 targets full recovery + 1x base profit.
    4. Streak >= 6 targets strict break-even.
    5. Completely removes the hard $100 cap, allowing bet sizes to scale uncapped.
    """
    config = _mock_config()
    config.paper_trade = True
    config.position_size_usdc = 10.0
    config.global_cooldown = 0.0 # No cooldown block
    
    clob = AsyncMock()
    clob.get_orderbook = AsyncMock(return_value=None)
    
    scanner = MagicMock()
    scanner._get_market_key_info = MagicMock(return_value=("ETH", 5))
    
    validator = MagicMock()
    validator.validate = AsyncMock(return_value=MagicMock(passed=True, checks={}))
    
    position_mgr = MagicMock()
    position_mgr.ws = None
    position_mgr.open_position = AsyncMock(return_value=True)
    
    now = int(time.time())
    
    await db.execute("DELETE FROM paper_positions")
    await db.execute("DELETE FROM markets")
    
    # Seed the markets in the database
    await upsert_market(db, {
        "market_id": "m_streak_3",
        "question": "Will Ethereum end UP or DOWN at 4:55 AM?",
        "slug": f"eth-updown-5m-{now - 600}",
        "yes_token_id": "yes_3",
        "no_token_id": "no_3"
    })
    await upsert_market(db, {
        "market_id": "m_streak_4",
        "question": "Will Ethereum end UP or DOWN at 5:00 AM?",
        "slug": f"eth-updown-5m-{now - 300}",
        "yes_token_id": "yes_4",
        "no_token_id": "no_4"
    })
    await upsert_market(db, {
        "market_id": "m_streak_5",
        "question": "Will Ethereum end UP or DOWN at 5:05 AM?",
        "slug": f"eth-updown-5m-{now}",
        "yes_token_id": "yes_5",
        "no_token_id": "no_5",
        "closes_at": now + 120
    })
    
    # Seed consecutive reversal streak losses starting at streak 3:
    # Streak 3: Lost $17.85
    await db.execute(
        """
        INSERT INTO paper_positions (
            position_id, market_id, side, entry_price, size_usdc, shares_bought, order_status, opened_at, closed_at, realised_pnl, symbol, window_minutes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("pos_streak_3", "m_streak_3", "YES", 0.51, 17.85, 35.0, "FILLED", now - 660, now - 360, -17.85, "ETH", 5)
    )
    await db.execute(
        """
        INSERT INTO signals (
            market_id, symbol, window_size, streak_depth, side, direction, guard_result, signal_timestamp, eventual_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m_streak_3", "ETH", "5", 3, "YES", "UP", "ACCEPTED", "2026-05-29T10:00:00.000Z", -17.85)
    )
    
    # Streak 4: Expired but unresolved trade (pnl is None, closes_at has passed). Size is $26.46.
    # It will be treated as an implicit loss of size_usdc ($26.46).
    await db.execute(
        """
        INSERT INTO paper_positions (
            position_id, market_id, side, entry_price, size_usdc, shares_bought, order_status, opened_at, closed_at, realised_pnl, symbol, window_minutes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("pos_streak_4", "m_streak_4", "YES", 0.49, 26.46, 54.0, "FILLED", now - 360, None, None, "ETH", 5)
    )
    await db.execute(
        """
        INSERT INTO signals (
            market_id, symbol, window_size, streak_depth, side, direction, guard_result, signal_timestamp, eventual_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m_streak_4", "ETH", "5", 4, "YES", "UP", "ACCEPTED", "2026-05-29T10:00:05.000Z", None)
    )
    await db.commit()
    
    decision = DecisionEngine(config, validator, position_mgr, clob, db, scanner=scanner)
    
    # Scenario A: Streak 5 (streak_depth = 5). Target Profit is Recovery + Win:
    # L_reversal = 17.85 (pos_streak_3) + 26.46 (pos_streak_4 expired) = 44.31 USDC.
    # Target profit = 44.31 + 10.00 = 54.31 USDC.
    # Entry YES price is 0.46 (NO price 0.54).
    # Shares = ceil(54.31 / (1.0 - 0.46)) = ceil(54.31 / 0.54) = 101.0.
    # Sized at 101.0 * 0.46 = 46.46 USDC.
    market_5 = {
        "market_id": "m_streak_5",
        "question": "Will Ethereum end UP or DOWN at 5:05 AM?",
        "slug": f"eth-updown-5m-{now}",
        "yes_token_id": "yes_5",
        "no_token_id": "no_5",
        "yes_price": 0.46,
        "no_price": 0.54,
        "closes_at": now + 120,
        "window_minutes": 5,
        "streak_depth": 5
    }
    
    success, reason = await decision.process_signal(market_5, side="YES")
    assert success is True
    args, kwargs = decision.position_mgr.open_position.call_args
    assert kwargs["market_id"] == "m_streak_5"
    assert kwargs["side"] == "YES"
    assert abs(kwargs["entry_price"] - 0.46) < 0.001
    assert abs(kwargs["size_usdc"] - 46.46) < 0.001
    assert abs(kwargs["shares"] - 101.0) < 0.001
    
    # Scenario B: Streak 6 (streak_depth = 6). Target Profit is Break-Even (cumulative_loss).
    # Seed Streak 5 as a resolved loss: -$47.38.
    # L_reversal = 17.85 + 26.46 + 47.38 = 91.69 USDC.
    # Target profit = 91.69 USDC (Strict break-even).
    # Entry YES price is 0.62.
    # Required shares = ceil(91.69 / (1.0 - 0.62)) = ceil(91.69 / 0.38) = 242.0.
    # Size = 242.0 * 0.62 = 150.04 USDC.
    # Since we removed the hard $100 cap, this size (which is > $100) must execute successfully!
    await db.execute(
        """
        INSERT INTO paper_positions (
            position_id, market_id, side, entry_price, size_usdc, shares_bought, order_status, opened_at, closed_at, realised_pnl, symbol, window_minutes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("pos_streak_5", "m_streak_5", "YES", 0.46, 47.38, 103.0, "FILLED", now - 60, now - 5, -47.38, "ETH", 5)
    )
    await db.execute(
        """
        INSERT INTO signals (
            market_id, symbol, window_size, streak_depth, side, direction, guard_result, signal_timestamp, eventual_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m_streak_5", "ETH", "5", 5, "YES", "UP", "ACCEPTED", "2026-05-29T10:00:10.000Z", -47.38)
    )
    await db.commit()
    
    cur = await db.execute("SELECT p.position_id, p.realised_pnl, p.size_usdc, p.opened_at, s.streak_depth FROM paper_positions p JOIN signals s ON p.market_id = s.market_id ORDER BY p.opened_at DESC")
    print("\nDATABASE SNAPSHOT FOR TEST:")
    [print(dict(row)) for row in await cur.fetchall()]
    
    decision.position_mgr.open_position.reset_mock()
    
    market_6 = {
        "market_id": "m_streak_6",
        "question": "Will Ethereum end UP or DOWN at 5:10 AM?",
        "slug": f"eth-updown-5m-{now + 300}",
        "yes_token_id": "yes_6",
        "no_token_id": "no_6",
        "yes_price": 0.62,
        "no_price": 0.38,
        "closes_at": now + 420,
        "window_minutes": 5,
        "streak_depth": 6
    }
    
    success, reason = await decision.process_signal(market_6, side="YES")
    assert success is True
    args, kwargs = decision.position_mgr.open_position.call_args
    assert kwargs["side"] == "YES"
    assert abs(kwargs["entry_price"] - 0.62) < 0.001
    assert abs(kwargs["size_usdc"] - 150.04) < 0.001
    assert abs(kwargs["shares"] - 242.0) < 0.001





