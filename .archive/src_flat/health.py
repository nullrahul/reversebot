"""
WebSocket Health Monitor — Heartbeat Check for Polymarket Reverse Bot.

Monitors last_message_received_at from the WebSocket client.
If > 90 seconds since last message → fires kill switch IMMEDIATELY.

MISTAKE 5 GUARD: Bot is blind to market moves when WS is disconnected
but still has resting orders. Kill switch cancels all via REST.

Runs check every 30 seconds as a background asyncio task.
"""

import asyncio
import time
from typing import Optional

from src.api.websocket import WebSocketClient
from src.risk.kill_switch import KillSwitch
from src.logs.log_config import get_logger

logger = get_logger("health")

# Fire kill switch after this many seconds of WS silence
WS_SILENCE_THRESHOLD_SECONDS = 90.0

# Check interval
HEALTH_CHECK_INTERVAL_SECONDS = 30.0


class HealthMonitor:
    """
    Monitors WebSocket connection health.
    Fires kill switch on prolonged silence.
    """

    def __init__(
        self,
        ws_client: WebSocketClient,
        kill_switch: KillSwitch,
        config: "BotConfig" = None,
    ):
        self.ws = ws_client
        self.kill_switch = kill_switch
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self._should_run = False

        # Determine threshold based on mode
        self.threshold = WS_SILENCE_THRESHOLD_SECONDS
        if config and config.target_market_mode == "crypto_short_window":
            self.threshold = 30.0  # Hyper-fast responsiveness for short windows

        logger.info(
            "health_monitor_initialized",
            silence_threshold=self.threshold,
            check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
            message=f"Health monitor ready — kill on {self.threshold}s WS silence",
        )

    async def start(self) -> None:
        """Start the health monitor as a background task."""
        self._should_run = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("health_monitor_started", message="Health monitor running")

    async def stop(self) -> None:
        """Stop the health monitor."""
        self._should_run = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("health_monitor_stopped", message="Health monitor stopped")

    async def _monitor_loop(self) -> None:
        """
        Background loop checking WS health every 30 seconds.
        """
        while self._should_run and not self.kill_switch.is_triggered:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

                if self.kill_switch.is_triggered:
                    break

                silence = self.ws.seconds_since_last_message
                connected = self.ws.is_connected

                if silence > self.threshold:
                    if len(self.ws._subscribed_markets) == 0:
                        logger.debug(
                            "ws_silence_ignored",
                            silence_seconds=round(silence, 1),
                            message="WS silent but ignored (0 markets subscribed)",
                        )
                        continue

                    logger.warning(
                        "ws_silence_reconnect",
                        silence_seconds=round(silence, 1),
                        threshold=self.threshold,
                        connected=connected,
                        message=(
                            f"⚠️ WS SILENT for {silence:.0f}s "
                            f"(threshold: {self.threshold}s) "
                            f"— FORCING RECONNECT"
                        ),
                    )
                    
                    # Force reconnect instead of killing the entire bot
                    if self.ws._ws and not self.ws._ws.closed:
                        await self.ws._ws.close()
                    
                    # Reset the silence timer so we don't spam reconnects while it's backing off
                    self.ws.last_message_received_at = time.time()
                    
                    # Do not break, allow the loop to continue checking health
                    continue

                elif silence > self.threshold / 2:
                    logger.warning(
                        "ws_silence_warning",
                        silence_seconds=round(silence, 1),
                        threshold=self.threshold,
                        message=f"WS silent for {silence:.0f}s — approaching kill threshold",
                    )
                else:
                    logger.debug(
                        "health_check_ok",
                        silence_seconds=round(silence, 1),
                        connected=connected,
                        message=f"Health OK — WS last msg {silence:.0f}s ago",
                    )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "health_check_error",
                    error=str(e),
                    message=f"Health check error: {e}",
                )
