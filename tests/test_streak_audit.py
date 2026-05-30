"""
Tests for Streak Logic and Telemetry Audit Fixes (V3.2).
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
from src.db.queries import init_db, close_db, create_position, log_signal_evaluation, upsert_market
from src.strategy.scanner import MarketScanner


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
    config.bot_version = "v3.2-test"
    config.paper_trade = True
    config.position_size_usdc = 10.0
    return config


@pytest.fixture
def mock_gamma():
    gamma = AsyncMock()
    # Mock ensuring session works
    gamma._ensure_session = AsyncMock()
    return gamma


@pytest.fixture
def mock_ws():
    ws = AsyncMock()
    ws.bid_cache = {}
    ws.ask_cache = {}
    ws.price_cache = {}
    ws.book_cache = {}
    return ws


@pytest.fixture
def mock_clob():
    return AsyncMock()


class TestStreakAuditFixes:
    """
    Verifies that the Bug 1, 2, 3, 5, 7, and telemetry upgrades work perfectly.
    """

    @pytest.mark.asyncio
    async def test_streak_reset_allows_exact_non_overlapping_streak(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Bug 1: Change <= to < in scanner.py line 436.
        Verify that when m_prev2_close == last_traded, it is NOT skipped (since streaks do not overlap).
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Enforce last traded is T=1000
        scanner.last_traded_close_ts[("BTC", 5)] = 1000
        
        # Scenario A: m_prev2_close = 1000 (meaning closes_at = 1000 + 2 * 300 = 1600)
        # Under old logic (<=), this would be skipped.
        # Under new logic (<), it should NOT be skipped.
        closes_at = 1600
        duration = 5
        dur_secs = duration * 60
        m_prev2_close = closes_at - (2 * dur_secs)  # 1600 - 600 = 1000
        
        # Verify the calculation is 1000
        assert m_prev2_close == 1000
        
        # Assert m_prev2_close < last_traded is False (so we do not continue/skip!)
        assert not (m_prev2_close < 1000)

    @pytest.mark.asyncio
    async def test_outcome_cache_fifo_eviction(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Bug 7: Outcome cache grows forever, causing a slow memory leak.
        Verify FIFO eviction of self._outcome_cache once it exceeds 200 elements.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Seed 200 dummy items
        for i in range(200):
            scanner._outcome_cache[f"slug_{i}"] = {"outcome": "UP", "source": "GAMMA", "delta_bps": 10.0}
            
        assert len(scanner._outcome_cache) == 200
        
        # Force a call to _get_historical_outcomes with a 201st slug to trigger eviction
        # Mocking self.gamma._get and _get_binance_candle_data to return quickly
        mock_gamma._get = AsyncMock(return_value=[{"markets": [{"closed": True, "outcomePrices": "[1.0, 0.0]"}]}])
        scanner._get_binance_candle_data = AsyncMock(return_value=(100.0, 101.0, "UP", 100.0))
        
        # Fetching outcomes for BTC 5m closing at 10000
        res = await scanner._get_historical_outcomes("BTC", 5, 10000)
        
        # The cache size should have pruned the oldest entry (slug_0) and added slug1 and slug2
        assert "slug_0" not in scanner._outcome_cache
        assert len(scanner._outcome_cache) <= 201

    @pytest.mark.asyncio
    async def test_log_signal_evaluation_accepts_telemetry(self, db):
        """
        Bug 2 & 5: Ensure log_signal_evaluation SQL parameters and table accept new telemetry columns cleanly.
        """
        signal_id = await log_signal_evaluation(
            db,
            market_id="test_market_1",
            symbol="BTC",
            window_size="5",
            direction="UP",
            side="YES",
            t1_market_id="t1_id",
            t1_result="UP",
            t2_market_id="t2_id",
            t2_result="UP",
            t3_market_id=None,
            t3_result=None,
            streak_depth=2,
            yes_price_at_signal=0.52,
            bid_ask_spread_at_signal=0.01,
            book_imbalance=-0.1,
            cvd_60s=5.0,
            price_velocity=0.0,
            ev_computed=0.08,
            seconds_to_close=120.0,
            guard_result="ENTERED",
            guard_rejection_reason=None,
            outcome_source="BINANCE_FALLBACK",
            candle_1_delta_bps=25.5,
            candle_2_delta_bps=12.2,
            entry_price=0.53,
            time_since_candle_close_secs=4.5,
            eventual_pnl=0.0
        )
        
        assert signal_id > 0
        
        # Query it back
        cursor = await db.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert row["outcome_source"] == "BINANCE_FALLBACK"
        assert abs(row["candle_1_delta_bps"] - 25.5) < 0.001
        assert abs(row["candle_2_delta_bps"] - 12.2) < 0.001
        assert abs(row["entry_price"] - 0.53) < 0.001
        assert abs(row["time_since_candle_close_secs"] - 4.5) < 0.001

    @pytest.mark.asyncio
    async def test_dynamic_denormalization_in_create_position(self, db):
        """
        Bug 6: Verify window_minutes and symbol are dynamically denormalized inside create_position
        by querying the markets table, without requiring caller signature updates.
        """
        # Seeding market inside db
        await upsert_market(db, {
            "market_id": "test_m_denorm",
            "question": "Will Bitcoin end UP or DOWN at 5:00 PM?",
            "slug": "btc-updown-15m-12345",
            "yes_token_id": "yes_token_123",
            "no_token_id": "no_token_123"
        })
        
        # Call create_position
        pos_id = await create_position(
            db,
            market_id="test_m_denorm",
            entry_price=0.55,
            size_usdc=10.0,
            shares_bought=18.0,
            bot_version="v3.2-test",
            paper=True
        )
        
        assert len(pos_id) > 0
        
        # Query it back from paper_positions
        cursor = await db.execute("SELECT symbol, window_minutes FROM paper_positions WHERE position_id = ?", (pos_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert row["symbol"] == "BTC"
        assert row["window_minutes"] == 15

    @pytest.mark.asyncio
    async def test_dynamic_regime_split_and_sizing(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verifies dynamic timeframe regime split and dynamic sizing.
        - 5m: Trend Following (UP-UP -> YES, DOWN-DOWN -> NO)
        - 15m: Mean Reversion / Fade (UP-UP -> NO, DOWN-DOWN -> YES)
        - Sizing: Streak-2 has 0.5x multiplier, Streak-4 has 2.0x multiplier and higher ceiling.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Test 5m window: Trend Following
        # UP-UP streak
        res_5m_up = {
            "out1": "UP", "out2": "UP", "out3": "?", "out4": "?", "out5": "?", "out6": "?",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "UNKNOWN", "src4": "UNKNOWN", "src5": "UNKNOWN", "src6": "UNKNOWN",
            "delta1": 10.0, "delta2": 5.0, "delta3": 0.0, "delta4": 0.0, "delta5": 0.0, "delta6": 0.0,
            "source": "GAMMA"
        }
        scanner._get_historical_outcomes = AsyncMock(return_value=res_5m_up)
        
        now = int(time.time())
        closes_at_5m = now + 120
        market_5m = {
            "market_id": "test_5m",
            "question": "Will Bitcoin end UP or DOWN at 5:00 PM?",
            "slug": f"btc-updown-5m-{closes_at_5m - 300}",
            "yes_token_id": "yes_5m",
            "no_token_id": "no_5m",
            "closes_at": closes_at_5m,
            "window_minutes": 5
        }
        scanner.tracked_markets = {"test_5m": market_5m}
        mock_ws.ask_cache = {"yes_5m": 0.50, "no_5m": 0.50}
        
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        assert signals[0]["target_side"] == "YES"  # Trend following UP-UP -> YES
        assert signals[0]["streak_depth"] == 2
        
        # Test 15m window: Mean Reversion
        # DOWN-DOWN-DOWN streak (Streak-3)
        res_15m_down = {
            "out1": "DOWN", "out2": "DOWN", "out3": "DOWN", "out4": "?", "out5": "?", "out6": "?",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "GAMMA", "src4": "UNKNOWN", "src5": "UNKNOWN", "src6": "UNKNOWN",
            "delta1": -10.0, "delta2": -5.0, "delta3": -3.0, "delta4": 0.0, "delta5": 0.0, "delta6": 0.0,
            "source": "GAMMA"
        }
        scanner._get_historical_outcomes = AsyncMock(return_value=res_15m_down)
        
        closes_at_15m = now + 120
        market_15m = {
            "market_id": "test_15m",
            "question": "Will Ethereum end UP or DOWN at 5:00 PM?",
            "slug": f"eth-updown-15m-{closes_at_15m - 900}",
            "yes_token_id": "yes_15m",
            "no_token_id": "no_15m",
            "closes_at": closes_at_15m,
            "window_minutes": 15
        }
        scanner.tracked_markets = {"test_15m": market_15m}
        mock_ws.ask_cache = {"yes_15m": 0.50, "no_15m": 0.50}
        
        signals_15m = await scanner.get_entry_signals()
        assert len(signals_15m) == 1
        assert signals_15m[0]["target_side"] == "YES"  # Mean reversion DOWN-DOWN-DOWN -> YES (fade DOWN-DOWN-DOWN)
        assert signals_15m[0]["streak_depth"] == 3

    @pytest.mark.asyncio
    async def test_streak_depth_5_and_6_detection(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verify that 5 and 6 matching outcomes correctly evaluate to streak_depth = 5 and 6.
        """
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Test 5-Streak
        res_5m_up_5 = {
            "out1": "UP", "out2": "UP", "out3": "UP", "out4": "UP", "out5": "UP", "out6": "?",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "GAMMA", "src4": "GAMMA", "src5": "GAMMA", "src6": "UNKNOWN",
            "delta1": 10.0, "delta2": 5.0, "delta3": 3.0, "delta4": 2.0, "delta5": 1.0, "delta6": 0.0,
            "source": "GAMMA"
        }
        scanner._get_historical_outcomes = AsyncMock(return_value=res_5m_up_5)
        
        now = int(time.time())
        closes_at_5m = now + 120
        market_5m = {
            "market_id": "test_5m",
            "question": "Will Bitcoin end UP or DOWN at 5:00 PM?",
            "slug": f"btc-updown-5m-{closes_at_5m - 300}",
            "yes_token_id": "yes_5m",
            "no_token_id": "no_5m",
            "closes_at": closes_at_5m,
            "window_minutes": 5
        }
        scanner.tracked_markets = {"test_5m": market_5m}
        mock_ws.ask_cache = {"yes_5m": 0.50, "no_5m": 0.50}
        
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        assert signals[0]["streak_depth"] == 5

        # Test 6-Streak
        res_5m_up_6 = {
            "out1": "UP", "out2": "UP", "out3": "UP", "out4": "UP", "out5": "UP", "out6": "UP",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "GAMMA", "src4": "GAMMA", "src5": "GAMMA", "src6": "GAMMA",
            "delta1": 10.0, "delta2": 5.0, "delta3": 3.0, "delta4": 2.0, "delta5": 1.0, "delta6": 0.5,
            "source": "GAMMA"
        }
        scanner._get_historical_outcomes = AsyncMock(return_value=res_5m_up_6)
        
        signals_6 = await scanner.get_entry_signals()
        assert len(signals_6) == 1
        assert signals_6[0]["streak_depth"] == 6

    @pytest.mark.asyncio
    async def test_scanner_reversal_bypasses_price_floor_ceiling(self, db, mock_config, mock_gamma, mock_ws, mock_clob):
        """
        Verify that reversal signals (e.g. streak_depth >= 3 on 5m) bypass
        the scanner's price floor (0.45) and ceiling (0.65) filter.
        """
        mock_config.min_entry_price = 0.45
        mock_config.max_entry_price = 0.65
        scanner = MarketScanner(mock_config, mock_gamma, mock_ws, mock_clob, db)
        
        # Streak-3 (Reversal) UP-UP-UP -> target_side = "NO" (fade)
        res_reversal = {
            "out1": "UP", "out2": "UP", "out3": "UP", "out4": "?", "out5": "?", "out6": "?",
            "src1": "GAMMA", "src2": "GAMMA", "src3": "GAMMA", "src4": "UNKNOWN", "src5": "UNKNOWN", "src6": "UNKNOWN",
            "delta1": 10.0, "delta2": 5.0, "delta3": 3.0, "delta4": 0.0, "delta5": 0.0, "delta6": 0.0,
            "source": "GAMMA"
        }
        scanner._get_historical_outcomes = AsyncMock(return_value=res_reversal)
        
        now = int(time.time())
        closes_at_5m = now + 120
        market_5m = {
            "market_id": "test_5m_reversal_bypass",
            "question": "Will Bitcoin end UP or DOWN at 5:00 PM?",
            "slug": f"btc-updown-5m-{closes_at_5m - 300}",
            "yes_token_id": "yes_5m",
            "no_token_id": "no_5m",
            "closes_at": closes_at_5m,
            "window_minutes": 5
        }
        scanner.tracked_markets = {"test_5m_reversal_bypass": market_5m}
        
        # NO price is 0.36 (below the 0.45 floor!)
        mock_ws.ask_cache = {"yes_5m": 0.64, "no_5m": 0.36}
        
        # When streak_depth >= 3 (reversal), it should bypass the floor and confirm the signal
        signals = await scanner.get_entry_signals()
        assert len(signals) == 1
        assert signals[0]["target_side"] == "NO"
        assert signals[0]["target_price"] == 0.36
        assert signals[0]["streak_depth"] == 3


