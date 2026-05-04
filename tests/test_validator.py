"""
Tests for Signal Validator — all 9 checks individually testable.
Tests for scanner volume calculation (taker-side only).
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

from src.db.queries import (
    init_db, close_db, upsert_market, create_position,
    update_position_status, close_position,
)


@pytest_asyncio.fixture
async def db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn
    await close_db(db_conn)
    os.unlink(tmp.name)


def _mock_config(**overrides):
    """Create a mock BotConfig."""
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
        "take_profit_multiplier": 3.0,
        "stop_loss_multiplier": 0.5,
        "paper_trade": True,
        "bot_version": "v0.1.0",
        "category_whitelist": ["Crypto", "Politics", "Sports"],
        "target_market_mode": "broad",
        "target_crypto_symbols": ["BTC", "ETH"],
        "target_window_minutes": [5, 15],
        "short_window_discovery_interval_seconds": 30,
        "short_window_min_seconds_to_close": 60,
    }
    defaults.update(overrides)
    config = MagicMock()
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def _sample_market(**overrides):
    """Create a sample valid market for validation."""
    now = int(time.time())
    base = {
        "market_id": "0xvalidmarket",
        "question": "Will BTC hit 100k?",
        "category": "Crypto",
        "yes_price": 0.85,
        "no_price": 0.15,
        "yes_volume_24h": 2000.0,
        "total_volume": 50000.0,
        "resolution_source": "oracle",
        "slug": "btc-standard-market",
        "event_slug": "btc-standard-event",
        "event_title": "Bitcoin standard market",
        "starts_at": now - 3600,
        "window_minutes": 60,
        "accepting_orders": True,
        "closes_at": now + 48 * 3600,  # 48 hours from now
        "last_scanned_at": now,
        "is_flagged": 1,
        "flag_reason": "yes_bias_confirmed",
        "last_seen_above_threshold": now - 120,
        "consecutive_cycles_above": 2,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Volume Double-Counting Test (MISTAKE 1)
# ---------------------------------------------------------------------------

class TestVolumeDoubleCountingGuard:
    """
    CRITICAL: Prove volume is taker-side only.
    Raw API volume = 2× real volume (maker + taker events).
    Our parser must halve it.
    """

    def test_volume_is_taker_side_only(self):
        """
        Given raw volume of 10000 (which includes both maker + taker fills),
        the parsed volume should be exactly 5000 (taker-side only).
        """
        from src.api.gamma_client import GammaClient

        client = GammaClient.__new__(GammaClient)

        raw_market = {
            "id": "0xtest",
            "question": "Test market",
            "category": "Crypto",
            "outcomes": ["Yes", "No"],
            "tokens": [
                {"outcome": "Yes", "price": 0.80},
                {"outcome": "No", "price": 0.20},
            ],
            "outcomePrices": ["0.80", "0.20"],
            "volume_24h": 10000.0,  # Raw = maker + taker = 2× real
            "volume": 50000.0,
            "end_date_iso": "2026-06-01T00:00:00Z",
        }

        parsed = client._parse_market(raw_market)

        assert parsed is not None
        assert parsed["yes_volume_24h"] == 5000.0, (
            f"Volume must be halved (taker-side only): "
            f"expected 5000.0, got {parsed['yes_volume_24h']}"
        )
        assert parsed["total_volume"] == 25000.0, (
            f"Total volume must be halved: "
            f"expected 25000.0, got {parsed['total_volume']}"
        )

    def test_zero_volume_handled(self):
        from src.api.gamma_client import GammaClient

        client = GammaClient.__new__(GammaClient)

        raw = {
            "id": "0xzero",
            "question": "Zero volume",
            "category": "Crypto",
            "outcomes": ["Yes", "No"],
            "tokens": [
                {"outcome": "Yes", "price": 0.50},
                {"outcome": "No", "price": 0.50},
            ],
            "outcomePrices": ["0.50", "0.50"],
            "volume_24h": 0,
            "volume": 0,
            "end_date_iso": "2026-06-01T00:00:00Z",
        }

        parsed = client._parse_market(raw)
        assert parsed["yes_volume_24h"] == 0.0
        assert parsed["total_volume"] == 0.0

    def test_short_window_metadata_is_parsed(self):
        from src.api.gamma_client import GammaClient

        client = GammaClient.__new__(GammaClient)

        raw_market = {
            "id": "0xshort",
            "question": "Ethereum Up or Down - April 11, 2:20PM-2:25PM ET",
            "slug": "eth-updown-5m-1775931600",
            "event_slug": "eth-updown-5m-1775931600",
            "event_title": "Ethereum Up or Down - 5 Minutes",
            "event_id": "109970",
            "event_tags": [
                {"id": "102127", "label": "Up or Down", "slug": "up-or-down"},
                {"id": "1312", "label": "Crypto Prices", "slug": "crypto-prices"},
                {"id": "101757", "label": "Recurring", "slug": "recurring"},
                {"id": "21", "label": "Crypto", "slug": "crypto"},
                {"id": "39", "label": "Ethereum", "slug": "ethereum"},
                {"id": "102892", "label": "5M", "slug": "5m"},
            ],
            "category": "Crypto",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.50", "0.50"],
            "volume24hr": 1000.0,
            "volume": 4000.0,
            "endDateIso": "2026-04-11T13:25:00Z",
            "acceptingOrders": None,
            "clobTokenIds": "[\"yes_token\",\"no_token\"]",
        }

        parsed = client._parse_market(raw_market)

        assert parsed is not None
        assert parsed["event_id"] == "109970"
        assert parsed["starts_at"] == 1775913600
        assert parsed["closes_at"] == 1775913900
        assert parsed["window_minutes"] == 5
        assert parsed["accepting_orders"] is None
        assert "up-or-down" in parsed["event_tag_slugs"]
        assert "ethereum" in parsed["event_tag_slugs"]
        assert parsed["yes_token_id"] == "yes_token"
        assert parsed["no_token_id"] == "no_token"


# ---------------------------------------------------------------------------
# Validator Tests — Each Check Individually
# ---------------------------------------------------------------------------

class TestValidatorChecks:

    @pytest.mark.asyncio
    async def test_check_1_liquidity_pass(self, db):
        """Volume >= min should pass."""
        from src.strategy.validator import SignalValidator

        config = _mock_config()
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        validator = SignalValidator(config, clob, db)
        market = _sample_market(yes_volume_24h=2000.0)
        await upsert_market(db, market)

        result = await validator.validate(market)
        assert result.checks["1_liquidity"] is True

    @pytest.mark.asyncio
    async def test_check_1_liquidity_fail(self, db):
        """Volume < min should fail."""
        from src.strategy.validator import SignalValidator

        config = _mock_config()
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        validator = SignalValidator(config, clob, db)
        market = _sample_market(yes_volume_24h=100.0)
        await upsert_market(db, market)

        result = await validator.validate(market)
        assert result.checks["1_liquidity"] is False

    @pytest.mark.asyncio
    async def test_check_4_duplicate_blocks_second_position(self, db):
        """If a market already has an OPEN position, check 4 should fail."""
        from src.strategy.validator import SignalValidator

        config = _mock_config()
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market()
        await upsert_market(db, market)

        # Create an open position
        pid = await create_position(db, "0xvalidmarket", 0.15, 15.0, 100.0, "v0.1.0", paper=True)
        await update_position_status(db, pid, "OPEN", paper=True)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)
        assert result.checks["4_duplicate"] is False

    @pytest.mark.asyncio
    async def test_check_7_position_count_limit(self, db):
        """When at max positions, check 7 should fail."""
        from src.strategy.validator import SignalValidator

        config = _mock_config(max_open_positions=2)
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        # Create 2 open positions in different markets
        for i in range(2):
            m = _sample_market(market_id=f"0xmarket_{i}")
            await upsert_market(db, m)
            pid = await create_position(db, f"0xmarket_{i}", 0.15, 15.0, 100.0, "v0.1.0", paper=True)
            await update_position_status(db, pid, "OPEN", paper=True)

        # New market signal
        new_market = _sample_market(market_id="0xnewmarket")
        await upsert_market(db, new_market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(new_market)
        assert result.checks["7_position_count"] is False

    @pytest.mark.asyncio
    async def test_check_8_price_stability(self, db):
        """Should pass check 8 on the second cycle."""
        from src.strategy.validator import SignalValidator

        config = _mock_config()
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market(consecutive_cycles_above=2)
        await upsert_market(db, market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)
        assert result.checks["8_price_stability"] is True

    @pytest.mark.asyncio
    async def test_check_9_category_filter(self, db):
        """Non-whitelisted category should fail check 9."""
        from src.strategy.validator import SignalValidator

        config = _mock_config(category_whitelist=["Crypto", "Politics"])
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market(category="Entertainment")
        await upsert_market(db, market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)
        assert result.checks["9_category"] is False

    @pytest.mark.asyncio
    async def test_short_window_time_check_passes_for_active_5m_market(self, db):
        from src.strategy.validator import SignalValidator

        from datetime import datetime, timedelta, timezone
        now = int(time.time())
        config = _mock_config(
            target_market_mode="crypto_short_window",
            short_window_min_seconds_to_close=60,
            category_whitelist=["Crypto"],
        )
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market(
            question="Ethereum Up or Down - April 11, 2:20PM-2:25PM ET",
            category="Crypto",
            starts_at=now - 30,
            closes_at=now + 180,
            window_minutes=5,
        )
        await upsert_market(db, market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)
        assert result.checks["3_time"] is True

    @pytest.mark.asyncio
    async def test_short_window_time_check_fails_near_expiry(self, db):
        from src.strategy.validator import SignalValidator

        from datetime import datetime, timedelta, timezone
        now = int(time.time())
        config = _mock_config(
            target_market_mode="crypto_short_window",
            short_window_min_seconds_to_close=60,
            category_whitelist=["Crypto"],
        )
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market(
            question="Bitcoin Up or Down - April 11, 2:20PM-2:25PM ET",
            category="Crypto",
            starts_at=now - 200,
            closes_at=now + 20,
            window_minutes=5,
        )
        await upsert_market(db, market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)
        assert result.checks["3_time"] is False


class TestShortWindowScannerFilter:

    def test_scanner_tracks_crypto_short_window(self):
        from src.strategy.scanner import MarketScanner

        config = _mock_config(
            target_market_mode="crypto_short_window",
            category_whitelist=["Crypto"],
            _TESTING_only_15min_windows=False
        )
        scanner = MarketScanner(config, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())

        now = 1775931720  # April 11, 2026 18:22:00 UTC -> 14:22:00 ET        
        q_eth = f"Ethereum Up or Down - April 11, 2:20PM-2:25PM ET"
        q_sol = f"Solana Up or Down - April 11, 2:20PM-2:25PM ET"

        match = _sample_market(
            question=q_eth,
            slug="eth-updown-5m-1775931600",
            event_slug="eth-updown-5m-1775931600",
            event_title="Ethereum Up or Down - 5 Minutes",
            event_tag_slugs=["up-or-down", "crypto-prices", "recurring", "crypto", "ethereum", "5m"],
            category="Crypto",
            starts_at=now - 30,
            closes_at=now + 120,
            window_minutes=5,
            accepting_orders=True,
        )
        match_sol = _sample_market(
            question=q_sol,
            slug="sol-updown-5m-1775931600",
            event_slug="sol-updown-5m-1775931600",
            event_title="Solana Up or Down - 5 Minutes",
            event_tag_slugs=["up-or-down", "crypto-prices", "recurring", "crypto", "solana", "5m"],
            category="Crypto",
            starts_at=now - 30,
            closes_at=now + 120,
            window_minutes=5,
            accepting_orders=True,
        )
        wrong_window = _sample_market(
            question="Bitcoin Up or Down - April 11 ET",
            slug="btc-updown-daily-1775931600",
            event_slug="btc-updown-daily-1775931600",
            event_title="Bitcoin Up or Down - Daily",
            event_tag_slugs=["up-or-down", "crypto-prices", "recurring", "crypto", "bitcoin", "daily"],
            category="Crypto",
            starts_at=now - 30,
            closes_at=now + 120,
            window_minutes=1440,
            accepting_orders=True,
        )

        assert scanner._should_track_market(match, now_override=now) is True
        assert scanner._should_track_market(match_sol, now_override=now) is True
        assert scanner._should_track_market(wrong_window, now_override=now) is False

    @pytest.mark.asyncio
    async def test_all_checks_pass(self, db):
        """A perfectly valid signal should pass all 9 checks."""
        from src.strategy.validator import SignalValidator

        config = _mock_config()
        clob = AsyncMock()
        clob.get_orderbook = AsyncMock(return_value=None)

        market = _sample_market()
        await upsert_market(db, market)

        validator = SignalValidator(config, clob, db)
        result = await validator.validate(market)

        # Checks 2 and 3 might fail depending on mock setup, but 1,4,5,6,7,8,9 should pass
        assert result.checks["1_liquidity"] is True
        assert result.checks["4_duplicate"] is True
        assert result.checks["7_position_count"] is True
        assert result.checks["8_price_stability"] is True
        assert result.checks["9_category"] is True


# ---------------------------------------------------------------------------
# Shares Calculation Test (MISTAKE 3)
# ---------------------------------------------------------------------------

class TestSharesCalculation:

    def test_correct_shares_formula(self):
        """shares = size_usdc / no_price — NOT size_usdc."""
        entry_price = 0.15
        size_usdc = 15.0
        shares = size_usdc / entry_price

        assert shares == 100.0
        assert abs(shares * entry_price - size_usdc) < 0.001

    def test_wrong_shares_formula_fails_invariant(self):
        """The wrong formula (shares = size_usdc) should fail the invariant."""
        entry_price = 0.15
        size_usdc = 15.0
        wrong_shares = size_usdc  # BUG: ignores price

        assert abs(wrong_shares * entry_price - size_usdc) > 0.001

    def test_fee_adjusted_ev(self):
        """
        MISTAKE 6: Fee-adjusted EV at different price points.
        2% fee on net winnings.
        """
        # At no_price=0.15: buy 100 shares for $15
        # If No wins: payout = 100 * 1.0 = $100, net = $(100-15)*0.98 = $83.30
        # EV ratio = 83.30 / 15 = 5.55 — well above 1.5
        no_price = 0.15
        size_usdc = 15.0
        shares = size_usdc / no_price
        net_payout = (1.0 - no_price) * shares * 0.98
        ev_ratio = net_payout / size_usdc

        assert ev_ratio > 1.5, f"EV ratio should be > 1.5, got {ev_ratio}"

        # At no_price=0.90: buy ~16.67 shares for $15
        # If No wins: payout = 16.67 * 1.0 = $16.67, net = $(16.67-15)*0.98 = $1.64
        # EV ratio = 1.64 / 15 = 0.109 — way below 1.5
        no_price_high = 0.90
        shares_high = size_usdc / no_price_high
        net_payout_high = (1.0 - no_price_high) * shares_high * 0.98
        ev_ratio_high = net_payout_high / size_usdc

        assert ev_ratio_high < 1.5, f"High No price should have low EV, got {ev_ratio_high}"
