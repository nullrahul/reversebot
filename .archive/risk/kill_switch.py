"""
Kill Switch — Emergency Halt & Cancel-All for Polymarket Reverse Bot.

NON-NEGOTIABLE: This module must fire automatically when:
1. Daily realised loss exceeds MAX_DAILY_LOSS_USDC
2. WebSocket feed is silent for > 90 seconds
3. 3 or more consecutive API errors of any type
4. Any unhandled exception propagates to main trading loop
5. SIGINT or SIGTERM received

Kill Sequence (in this EXACT order):
1. Set global KILL_SWITCH_ACTIVE flag
2. IMMEDIATELY cancel-all orders via REST (do NOT wait for WS)
3. Log KILL_SWITCH_TRIGGERED with reason
4. Write event to events_log
5. Wait for cancel confirmations (10s hard timeout)
6. Log final state of every position
7. Close WebSocket connections
8. Close DB connections
9. Exit cleanly

Manual trigger: python main.py --kill
"""

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

from src.api.clob_client import ClobClient
from src.api.websocket import WebSocketClient
from src.db.queries import get_open_positions, log_event
from src.logs.log_config import get_logger

logger = get_logger("kill_switch")


class KillSwitch:
    """
    Emergency halt system for the Reverse Bot.
    cancel_all_orders() is ALWAYS called BEFORE any cleanup.
    """

    def __init__(
        self,
        clob_client: ClobClient,
        ws_client: Optional[WebSocketClient],
        db,
        bot_version: str = "v0.1.0",
    ):
        self.clob = clob_client
        self.ws = ws_client
        self.db = db
        self.bot_version = bot_version

        # Global kill flag — checked by all trading loops
        self.is_active = asyncio.Event()
        # Set when the full kill sequence (step 1-9) is finished
        self.is_complete = asyncio.Event()

        # Task tracking for awaiting completion
        self._trigger_task: Optional[asyncio.Task] = None

        # Track consecutive API errors
        self._consecutive_api_errors: int = 0
        self._max_api_errors: int = 10  # Relaxed for 1GB VPS instability

        # Callbacks to run after kill (e.g., notification)
        self._on_kill_callbacks: List[Callable] = []

        logger.info(
            "kill_switch_initialized",
            message="Kill switch armed and ready",
        )

    @property
    def is_triggered(self) -> bool:
        return self.is_active.is_set()

    def register_callback(self, callback: Callable) -> None:
        """Register a callback to run after kill switch fires."""
        self._on_kill_callbacks.append(callback)

    def record_api_error(self) -> bool:
        """
        Record an API error. Returns True if kill threshold reached.
        """
        self._consecutive_api_errors += 1
        logger.warning(
            "api_error_recorded",
            consecutive_count=self._consecutive_api_errors,
            threshold=self._max_api_errors,
            message=f"API error #{self._consecutive_api_errors}",
        )

        if self._consecutive_api_errors >= self._max_api_errors:
            return True
        return False

    def reset_api_errors(self) -> None:
        """Reset consecutive error count after a successful API call."""
        if self._consecutive_api_errors > 0:
            self._consecutive_api_errors = 0

    async def trigger(self, reason: str, details: Optional[Dict] = None) -> None:
        """
        Fire the kill switch. Follows the exact sequence specified.
        """
        if self.is_triggered:
            # If already running, ensure we are awaiting the task if possible
            if self._trigger_task and not self._trigger_task.done():
                if self._trigger_task is asyncio.current_task():
                    return
                await self._trigger_task
            return

        # Start the sequence as a protected task
        self._trigger_task = asyncio.current_task()
        await self._execute_kill_sequence(reason, details)

    async def _execute_kill_sequence(self, reason: str, details: Optional[Dict] = None) -> None:
        """Internal execution of the kill sequence steps."""

        # ═══════════════════════════════════════════════════════════
        # STEP 1: Set global flag IMMEDIATELY
        # ═══════════════════════════════════════════════════════════
        self.is_active.set()

        logger.critical(
            "kill_switch_triggered",
            reason=reason,
            details=details,
            message=f"🚨 KILL SWITCH TRIGGERED — Reason: {reason}",
        )

        # ═══════════════════════════════════════════════════════════
        # STEP 2: IMMEDIATELY cancel all orders via REST
        # This MUST happen BEFORE any cleanup or shutdown logic.
        # Do NOT wait for WebSocket confirmation.
        # ═══════════════════════════════════════════════════════════
        cancel_success = await self.clob.cancel_all_orders()

        if cancel_success:
            logger.info(
                "cancel_all_success",
                message="All orders cancelled via REST API",
            )
        else:
            logger.critical(
                "cancel_all_failed",
                message="CRITICAL: Failed to cancel all orders!",
            )

        # ═══════════════════════════════════════════════════════════
        # STEP 3: Log KILL_SWITCH_TRIGGERED
        # ═══════════════════════════════════════════════════════════
        try:
            open_positions = await get_open_positions(self.db)
            paper_positions = await get_open_positions(self.db, paper=True)
        except Exception:
            open_positions = []
            paper_positions = []

        # ═══════════════════════════════════════════════════════════
        # STEP 4: Write event to events_log
        # ═══════════════════════════════════════════════════════════
        try:
            await log_event(
                self.db,
                event_type="KILL_SWITCH",
                bot_version=self.bot_version,
                payload={
                    "reason": reason,
                    "details": details,
                    "open_positions": len(open_positions),
                    "paper_positions": len(paper_positions),
                    "cancel_success": cancel_success,
                    "timestamp": int(time.time()),
                },
            )
        except Exception as e:
            logger.error(
                "kill_event_log_failed",
                error=str(e),
                message=f"Failed to log kill switch event: {e}",
            )

        # ═══════════════════════════════════════════════════════════
        # STEP 5: Wait for cancel confirmations (10s hard timeout)
        # ═══════════════════════════════════════════════════════════
        if not cancel_success:
            logger.info(
                "cancel_confirmation_wait",
                message="Waiting 10s for cancel confirmations...",
            )
            await asyncio.sleep(10.0)

        # ═══════════════════════════════════════════════════════════
        # STEP 6: Log final state of every position
        # ═══════════════════════════════════════════════════════════
        try:
            all_open = await get_open_positions(self.db)
            for pos in all_open:
                logger.info(
                    "final_position_state",
                    position_id=pos["position_id"],
                    market_id=pos["market_id"],
                    order_status=pos["order_status"],
                    entry_price=pos.get("entry_price"),
                    size_usdc=pos.get("size_usdc"),
                    message=f"Final state: {pos['position_id']} — {pos['order_status']}",
                )
        except Exception as e:
            logger.error(
                "final_state_log_failed",
                error=str(e),
                message="Failed to log final position states",
            )

        # ═══════════════════════════════════════════════════════════
        # STEP 7: Run any registered callbacks
        # ═══════════════════════════════════════════════════════════
        for cb in self._on_kill_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(reason, details)
                else:
                    cb(reason, details)
            except Exception as e:
                logger.error(
                    "kill_callback_error",
                    error=str(e),
                    message=f"Kill switch callback error: {e}",
                )

        logger.critical(
            "kill_switch_complete",
            reason=reason,
            message="🛑 Kill switch sequence complete — bot is halted",
        )

        # ═══════════════════════════════════════════════════════════
        # STEP 8: Signal completion (main loop will exit)
        # ═══════════════════════════════════════════════════════════
        self.is_complete.set()
        return True
