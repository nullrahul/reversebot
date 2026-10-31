"""
Unit tests for the rolling Streak Quality Score (Option A) passive diagnostic guard.
"""

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.config import BotConfig
from src.db.queries import init_db, close_db, upsert_market
from src.strategy.scanner import MarketScanner
from src.strategy.decision import DecisionEngine


@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    try:
        await close_db(db_conn)
    except Exception:
        pass
    os.unlink(tmp.name)


@pytest.fixture
def mock_config():
    config = MagicMock(spec=BotConfig)
    config.bot_version = "v3.102-test"
    config.paper_trade = True
    config.position_size_usdc = 10.0
    config.min_streak_quality_score = 0.45
    config.streak_quality_window = 7
    config.yes_bias_threshold = 0.80
    config.yes_bias_max_threshold = 0.97
    config.min_volume_24h_usdc = 500.0
    config.min_time_to_close_hours = 6
    config.max_time_to_close_days = 90
    config.max_open_positions = 5
    config.max_daily_loss_usdc = 75.0
    config.max_total_exposure_usdc = 100.0
    config.category_whitelist = ["Crypto"]
    config.target_market_mode = "crypto_short_window"
    config.target_crypto_symbols = ["BTC", "ETH"]
    config.target_window_minutes = [5, 15]
    config.early_yes_bias_max = 0.95
    config.mid_yes_bias_max = 0.95
    config.late_yes_bias_max = 0.95
    config.early_phase_min_seconds = 600
    config.mid_phase_min_seconds = 180
    config.max_no_ask_multiplier = 2.0
    config.high_vol_assets = []
    config.high_vol_ask_multiplier = 2.0
    config.global_cooldown = 0.0
    config.min_ev_threshold = 0.0
    config.max_spread_pct_of_ask = 0.60
    config.timeframe_15m_enabled = True
    config.min_entry_price = 0.45
    config.max_entry_price = 0.65
    return config


@pytest.fixture
def mock_gamma():
    gamma = AsyncMock()
    gamma._ensure_session = AsyncMock()
    return gamma


@pytest.fixture
def mock_ws():
    ws = AsyncMock()
    ws.bid_cache = {}
    ws.ask_cache = {}
    ws.price_cache = {}
    ws.book_cache = {}
    
    def mock_get_best_ask(token_id):
        if ws.ask_cache.get(token_id) is not None:
            return ws.ask_cache[token_id]
        return None

    def mock_get_best_bid(token_id):
        if ws.bid_cache.get(token_id) is not None:
            return ws.bid_cache[token_id]
        return None

    ws.get_best_ask = mock_get_best_ask
    ws.get_best_bid = mock_get_best_bid
    return ws


@pytest.fixture
def mock_clob():
    return AsyncMock()


class TestStreakQualityScore:

    @pytest.mark.asyncio
    async def test_streak_quality_trending(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verify that a perfectly trending market (all outcomes same) yields a quality score of 1.0.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Test a strong historical trend (DOWN) that recently reversed into a UP Streak 2.
        # So out1=UP, out2=UP (the streak). out3=DOWN (breaks the streak, sets depth=2).
        # Then historical transitions are out3..out8 all DOWN.
        res_trending = {
            "out1": "UP", "out2": "UP", "out3": "DOWN", "out4": "DOWN", "out5": "DOWN", "out6": "DOWN", "out7": "DOWN", "out8": "DOWN",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "GAMMA", "src4": "GAMMA", "src5": "GAMMA", "src6": "GAMMA", "src7": "GAMMA", "src8": "GAMMA",
            "delta1": 10.0, "delta2": 5.0, "delta3": 3.0, "delta4": 2.0, "delta5": 1.0, "delta6": 1.0, "delta7": 1.0, "delta8": 1.0,
            "source": "GAMMA"
        }
        for i in range(9, 31):
            res_trending[f"out{i}"] = "?"
            res_trending[f"src{i}"] = "UNKNOWN"
            res_trending[f"delta{i}"] = 0.0

        scanner._get_historical_outcomes = AsyncMock(return_value=res_trending)
        
        now = int(time.time())
        closes_at = now + 120
        market = {
            "market_id": "test_m_trending",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "slug": f"btc-updown-5m-{closes_at - 300}",
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
            "closes_at": closes_at,
            "window_minutes": 5,
            "yes_price": 0.50,
            "no_price": 0.50
        }
        scanner.tracked_markets = {"test_m_trending": market}
        
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        # i=2: UP vs DOWN (same=0)
        # i=3: DOWN vs DOWN (same=1)
        # i=4: DOWN vs DOWN (same=2)
        # i=5: DOWN vs DOWN (same=3)
        # i=6: DOWN vs DOWN (same=4)
        # i=7: DOWN vs DOWN (same=5)
        # Total valid = 6. Score = 5 / 6 = 0.8333...
        assert abs(signals[0]["streak_quality_score"] - (5.0 / 6.0)) < 0.0001

    @pytest.mark.asyncio
    async def test_streak_quality_oscillating(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verify that an oscillating market yields a quality score of 0.0 under the new pre-streak measurement offset.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # out1=UP, out2=UP (Streak 2)
        # out3=DOWN, out4=UP, out5=DOWN, out6=UP, out7=DOWN, out8=UP
        res_oscillating = {
            "out1": "UP", "out2": "UP", "out3": "DOWN", "out4": "UP", "out5": "DOWN", "out6": "UP", "out7": "DOWN", "out8": "UP",
            "source": "GAMMA"
        }
        for i in range(1, 9):
            res_oscillating[f"src{i}"] = "GAMMA"
            res_oscillating[f"delta{i}"] = 1.0
        for i in range(9, 31):
            res_oscillating[f"out{i}"] = "?"
            res_oscillating[f"src{i}"] = "UNKNOWN"
            res_oscillating[f"delta{i}"] = 0.0

        scanner._get_historical_outcomes = AsyncMock(return_value=res_oscillating)
        
        now = int(time.time())
        closes_at = now + 120
        market = {
            "market_id": "test_m_oscillating",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "slug": f"btc-updown-5m-{closes_at - 300}",
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
            "closes_at": closes_at,
            "window_minutes": 5,
            "yes_price": 0.50,
            "no_price": 0.50
        }
        scanner.tracked_markets = {"test_m_oscillating": market}
        
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        # i=2: UP vs DOWN (0)
        # i=3: DOWN vs UP (0)
        # i=4: UP vs DOWN (0)
        # i=5: DOWN vs UP (0)
        # i=6: UP vs DOWN (0)
        # i=7: DOWN vs UP (0)
        # 6 valid transitions, 0 same
        assert abs(signals[0]["streak_quality_score"] - 0.0) < 0.0001

    @pytest.mark.asyncio
    async def test_streak_quality_insufficient_transitions(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verify that if there are fewer than 5 valid transitions (e.g. "?" present in lookback window),
        the streak quality score evaluates to None.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Only 4 resolved outcomes, others are "?"
        res_insufficient = {
            "out1": "UP", "out2": "UP", "out3": "UP", "out4": "UP", "out5": "?", "out6": "?", "out7": "?", "out8": "?",
            "source": "GAMMA"
        }
        for i in range(1, 5):
            res_insufficient[f"src{i}"] = "GAMMA"
            res_insufficient[f"delta{i}"] = 1.0
        for i in range(5, 31):
            res_insufficient[f"out{i}"] = "?"
            res_insufficient[f"src{i}"] = "UNKNOWN"
            res_insufficient[f"delta{i}"] = 0.0

        scanner._get_historical_outcomes = AsyncMock(return_value=res_insufficient)
        
        now = int(time.time())
        closes_at = now + 120
        market = {
            "market_id": "test_m_insufficient",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "slug": f"btc-updown-5m-{closes_at - 300}",
            "yes_token_id": "yes_t",
            "no_token_id": "no_t",
            "closes_at": closes_at,
            "window_minutes": 5,
            "yes_price": 0.50,
            "no_price": 0.50
        }
        scanner.tracked_markets = {"test_m_insufficient": market}
        
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        assert signals[0]["streak_quality_score"] is None

    @pytest.mark.asyncio
    async def test_streak_quality_decision_logs_would_reject(self, db, mock_config):
        """
        Verify that process_signal registers a warning/bypass for streak quality below threshold,
        but successfully proceeds (returns success = True).
        """
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)
        scanner = MagicMock()
        scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
        scanner.last_traded_close_ts = {}
        validator = MagicMock()
        position_mgr = MagicMock()
        position_mgr.ws = None
        
        now = int(time.time())
        await upsert_market(db, {
            "market_id": "test_m_dec_soft",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
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
        })
        
        decision = DecisionEngine(mock_config, validator, position_mgr, clob, db, scanner=scanner)
        decision.soft_audit_db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        decision.rejection_logger = MagicMock()
        decision.rejection_logger.log_rejection = AsyncMock()
        
        validation_res = MagicMock()
        validation_res.passed = True
        validation_res.checks = {}
        validation_res.reasons = []
        validation_res.ev_ratio = 2.0
        decision.validator.validate = AsyncMock(return_value=validation_res)
        decision.position_mgr.open_position = AsyncMock(return_value=True)

        # Signal payload with quality score 0.35 (below 0.45 threshold)
        market = {
            "market_id": "test_m_dec_soft",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "yes_price": 0.50,
            "no_price": 0.50,
            "window_minutes": 5,
            "streak_quality_score": 0.35,
        }
        
        success, reason = await decision.process_signal(market, side="YES")
        assert success is True
        assert "placed successfully" in reason
        
        # Verify it was passed to rejection logger
        decision.rejection_logger.log_rejection.assert_called_once()
        args, kwargs = decision.rejection_logger.log_rejection.call_args
        assert "STREAK_QUALITY_0.3500_LT_0.45" in kwargs["failed_checks"]
        assert "WOULD_HAVE_BLOCKED" in kwargs["primary_reason"]
        
        # Cleanup soft audit file
        if os.path.exists(decision.soft_audit_db_path):
            os.unlink(decision.soft_audit_db_path)

    @pytest.mark.asyncio
    async def test_streak_quality_decision_passes_above_threshold(self, db, mock_config):
        """
        Verify that process_signal does not record any warning when the streak quality score
        is above or equal to the threshold.
        """
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)
        scanner = MagicMock()
        scanner._get_market_key_info = MagicMock(return_value=("BTC", 5))
        scanner.last_traded_close_ts = {}
        validator = MagicMock()
        position_mgr = MagicMock()
        position_mgr.ws = None
        
        now = int(time.time())
        await upsert_market(db, {
            "market_id": "test_m_dec_pass",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "category": "Crypto",
            "yes_price": 0.50,
            "no_price": 0.50,
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
        })
        
        decision = DecisionEngine(mock_config, validator, position_mgr, clob, db, scanner=scanner)
        decision.soft_audit_db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        decision.rejection_logger = MagicMock()
        decision.rejection_logger.log_rejection = AsyncMock()
        
        validation_res = MagicMock()
        validation_res.passed = True
        validation_res.checks = {}
        validation_res.reasons = []
        validation_res.ev_ratio = 2.0
        decision.validator.validate = AsyncMock(return_value=validation_res)
        decision.position_mgr.open_position = AsyncMock(return_value=True)

        # Signal payload with quality score 0.70 (above 0.45 threshold)
        market = {
            "market_id": "test_m_dec_pass",
            "question": "Will BTC end UP or DOWN - 5 Minutes?",
            "yes_price": 0.50,
            "no_price": 0.50,
            "window_minutes": 5,
            "streak_quality_score": 0.70,
        }
        
        success, reason = await decision.process_signal(market, side="YES")
        assert success is True
        
        # Rejection logger should not be called since there are no warning/would_reject checks triggered
        decision.rejection_logger.log_rejection.assert_not_called()
        
        # Cleanup soft audit file
        if os.path.exists(decision.soft_audit_db_path):
            os.unlink(decision.soft_audit_db_path)
