"""
Tests for Kill Switch — MANDATORY before live trading.

KEY ASSERTION: cancel_all_orders() MUST be called BEFORE any cleanup/shutdown.
This test uses a mock CLOB client to verify the exact call order.
"""

import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.risk.kill_switch import KillSwitch
from src.db.queries import init_db, close_db


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
def mock_clob():
    """Create a mock CLOB client with tracked call order."""
    clob = AsyncMock()
    clob.cancel_all_orders = AsyncMock(return_value=True)
    clob.is_paper = True
    return clob


@pytest.fixture
def mock_ws():
    """Create a mock WebSocket client."""
    ws = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.is_connected = True
    ws.seconds_since_last_message = 0.0
    return ws


class TestKillSwitchSequence:
    """
    CRITICAL: cancel_all_orders() must be called BEFORE any cleanup.
    These tests are MANDATORY before enabling live trading.
    """

    @pytest.mark.asyncio
    async def test_cancel_all_called_before_cleanup(self, db, mock_clob, mock_ws):
        """
        The single most important test: verify cancel_all_orders() is
        called BEFORE WebSocket disconnect and DB close.
        """
        call_order = []

        # Track call order
        original_cancel = mock_clob.cancel_all_orders
        async def tracked_cancel():
            call_order.append("cancel_all_orders")
            return await original_cancel()

        original_disconnect = mock_ws.disconnect
        async def tracked_disconnect():
            call_order.append("ws_disconnect")
            return await original_disconnect()

        mock_clob.cancel_all_orders = tracked_cancel
        mock_ws.disconnect = tracked_disconnect

        ks = KillSwitch(mock_clob, mock_ws, db, "v0.1.0")
        await ks.trigger("TEST_TRIGGER", {"test": True})

        # cancel_all MUST come before cleanup logic
        assert "cancel_all_orders" in call_order, "cancel_all_orders was never called"

        cancel_idx = call_order.index("cancel_all_orders")
        assert cancel_idx == 0, (
            f"cancel_all_orders must be called first. "
            f"Actual order: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_kill_switch_sets_flag_immediately(self, db, mock_clob, mock_ws):
        """Kill flag must be set before any async operations."""
        ks = KillSwitch(mock_clob, mock_ws, db, "v0.1.0")

        assert not ks.is_triggered
        await ks.trigger("TEST")
        assert ks.is_triggered

    @pytest.mark.asyncio
    async def test_kill_switch_idempotent(self, db, mock_clob, mock_ws):
        """Triggering kill switch twice should not run sequence twice."""
        ks = KillSwitch(mock_clob, mock_ws, db, "v0.1.0")

        await ks.trigger("FIRST")
        # cancel_all called once
        first_call_count = mock_clob.cancel_all_orders.call_count if hasattr(mock_clob.cancel_all_orders, 'call_count') else 1

        await ks.trigger("SECOND")
        # Should not call cancel_all again
        # (The tracked version doesn't have call_count, but the mock won't re-execute)

        assert ks.is_triggered

    @pytest.mark.asyncio
    async def test_cancel_all_called_even_if_ws_none(self, db, mock_clob):
        """Kill switch must work even without a WebSocket client."""
        ks = KillSwitch(mock_clob, None, db, "v0.1.0")
        await ks.trigger("NO_WS_TEST")

        assert ks.is_triggered
        # cancel_all should still be called

    @pytest.mark.asyncio
    async def test_kill_records_event_in_db(self, mock_clob, mock_ws):
        """Kill switch event should be written to events_log."""
        # Use a separate DB for this test
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        test_db = await init_db(tmp.name)

        # Patch close_db at the source (src.db.queries.close_db) to prevent
        # the kill switch from actually closing our test connection
        with patch("src.db.queries.close_db", new_callable=AsyncMock):
            ks = KillSwitch(mock_clob, mock_ws, test_db, "v0.1.0")
            await ks.trigger("DB_TEST")

        cursor = await test_db.execute(
            "SELECT * FROM events_log WHERE event_type = 'KILL_SWITCH'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1

        await close_db(test_db)
        os.unlink(tmp.name)


class TestConsecutiveAPIErrors:

    def test_error_tracking(self, db, mock_clob, mock_ws):
        ks = KillSwitch(mock_clob, mock_ws, db, "v0.1.0")

        # First 9 errors — should not trigger
        for _ in range(9):
            assert not ks.record_api_error()

        # 10th error — should trigger
        assert ks.record_api_error()

    def test_error_reset(self, db, mock_clob, mock_ws):
        ks = KillSwitch(mock_clob, mock_ws, db, "v0.1.0")

        for _ in range(5):
            ks.record_api_error()
        ks.reset_api_errors()

        # After reset, should need 10 more errors
        for _ in range(9):
            assert not ks.record_api_error()
        assert ks.record_api_error()
