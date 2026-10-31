import os
import pytest
import asyncio
import sqlite3
from unittest.mock import patch, AsyncMock, MagicMock
from src.strategy.regime_tracker import RegimeTracker

DB_PATH = "data/test_regime_telemetry.db"


@pytest.fixture
def clean_db():
    # Remove test DB if it exists
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    yield
    # Cleanup after test
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db_dir = os.path.dirname(DB_PATH)
    if os.path.exists(db_dir) and not os.listdir(db_dir):
        os.rmdir(db_dir)


def generate_mock_klines(count, pattern="alternating"):
    """
    Generate mock kline lists.
    Each kline: [open_time, open_price, high, low, close_price, volume, close_time, ...]
    """
    klines = []
    base_time = 1700000000000  # Unix timestamp ms
    for i in range(count):
        open_time = base_time + i * 300000  # 5 minutes in ms
        # alternating pattern to calculate deterministic score
        if pattern == "alternating":
            # UP if even index, DOWN if odd index
            # directions: [1, -1, 1, -1, ...]
            # continuations: 0 (no two consecutive are the same)
            open_price = 100.0
            close_price = 101.0 if i % 2 == 0 else 99.0
        elif pattern == "all_up":
            # directions: [1, 1, 1, 1, ...]
            # continuations: count - 1
            open_price = 100.0
            close_price = 101.0
        else:  # half up half down
            # first half UP, second half DOWN
            # e.g. [1, 1, ..., 1, -1, -1, ..., -1]
            open_price = 100.0
            close_price = 101.0 if i < count // 2 else 99.0

        klines.append([
            open_time,
            str(open_price),
            "102.0",
            "98.0",
            str(close_price),
            "10.0",
            open_time + 299999,
            "1000.0",
            100,
            "5.0",
            "500.0",
            "0"
        ])
    return klines


@pytest.mark.asyncio
async def test_regime_tracker_calculations():
    tracker = RegimeTracker(db_path=DB_PATH)

    # 1. Test calculation logic for alternating pattern
    # 31 closed candles -> 30 transitions
    klines_alt = generate_mock_klines(31, "alternating")
    result_alt = tracker._calculate_persistence(klines_alt)
    assert (result_alt["total_after_up"] + result_alt["total_after_down"]) == 30
    assert result_alt["up_continuations"] == 0
    assert result_alt["down_continuations"] == 0
    assert result_alt["overall"] == 0.0

    # 2. Test calculation logic for all_up pattern
    klines_up = generate_mock_klines(31, "all_up")
    result_up = tracker._calculate_persistence(klines_up)
    assert (result_up["total_after_up"] + result_up["total_after_down"]) == 30
    assert result_up["up_continuations"] == 30
    assert result_up["overall"] == 1.0

    # 3. Test calculation logic for half_up_half_down pattern
    # e.g., 15 UP, then 16 DOWN
    # directions: [1, ..., 1 (15 times), -1, ..., -1 (16 times)]
    # transitions: 30
    # continuations: 14 (within first half) + 15 (within second half) = 29
    # transitions that reversed: index 15 (1 -> -1)
    klines_half = generate_mock_klines(31, "half")
    result_half = tracker._calculate_persistence(klines_half)
    assert (result_half["total_after_up"] + result_half["total_after_down"]) == 30
    assert result_half["up_continuations"] == 14
    assert result_half["down_continuations"] == 15
    assert result_half["overall"] == 0.9667


@pytest.mark.asyncio
async def test_regime_tracker_initial_fetch_and_polling(clean_db):
    tracker = RegimeTracker(db_path=DB_PATH)

    # Generate mock responses
    # startup fetch needs limit=32 -> return 32 klines
    mock_klines_startup = generate_mock_klines(32, "all_up")

    # mock Binance API response
    async def mock_fetch(*args, **kwargs):
        return mock_klines_startup

    async def dummy_poll_loop():
        pass

    with patch.object(tracker, "_fetch_klines_from_binance", side_effect=mock_fetch):
        # Start the tracker with a dummy poll loop to prevent MagicMock await errors
        with patch.object(tracker, "_poll_loop", side_effect=dummy_poll_loop):
            await tracker.start()
            assert tracker._running is True

        # Check database table initialization
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='persistence_history'")
        assert cursor.fetchone() is not None
        conn.close()

        # Initial scores should be loaded (all_up -> 1.0)
        assert tracker.get_current_score("BTC", 5) == 1.0
        assert tracker.get_current_score("ETH", 5) == 1.0

        # Now test polling when a new candle closes
        # We will mock the next API request returning 33 klines
        mock_klines_poll = generate_mock_klines(33, "all_up")

        async def mock_fetch_poll(*args, **kwargs):
            return mock_klines_poll

        with patch.object(tracker, "_fetch_klines_from_binance", side_effect=mock_fetch_poll):
            await tracker._update_scores()

            # The score recalculated should still be 1.0
            assert tracker.get_current_score("BTC", 5) == 1.0

            # It should have written a record to sqlite database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM persistence_history")
            assert cursor.fetchone()[0] == 2  # 1 for BTC, 1 for ETH
            
            cursor.execute("SELECT asset, persistence_score, continuations, transitions FROM persistence_history LIMIT 1")
            row = cursor.fetchone()
            assert row[0] in ("BTC", "ETH")
            assert row[1] == 1.0
            assert row[2] == 29
            assert row[3] == 29
            conn.close()

        # Clean stop
        await tracker.stop()
        assert tracker._running is False
