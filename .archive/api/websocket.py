"""
WebSocket Client for Polymarket CLOB real-time feeds.

Subscribes to: book, price_change, user (authenticated)

Features:
- Exponential backoff reconnect: 1s → 2s → 4s → 8s → 16s → 30s max
- Tracks last_message_received_at for health monitoring
- asyncio.Queue for thread-safe event passing to main loop
- Separate asyncio.Task — never blocks the trading loop
- Re-subscribes to all channels on reconnect

MISTAKE 5 GUARD: health.py monitors last_message_received_at.
If > 90s since last message → kill switch fires automatically.
"""

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp

from src.logs.log_config import get_logger

logger = get_logger("websocket")

# Backoff schedule: 1s → 2s → 4s → 8s → 16s → 30s max
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
BACKOFF_FACTOR = 2.0


class WebSocketClient:
    """
    Async WebSocket client for Polymarket CLOB feeds.
    Handles connection lifecycle, reconnection, and event distribution.
    """

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        auth_headers: Optional[Dict[str, str]] = None,
    ):
        self._ws_url = ws_url
        self._auth_headers = auth_headers or {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

        # In-memory price cache for fast scanner access
        self.price_cache: Dict[str, float] = {}
        self.token_to_market: Dict[str, str] = {}
        
        # Health tracking
        self.last_message_received_at: float = time.time()

        # Connection state
        self._connected = False
        self._should_run = False
        self._reconnect_attempt = 0

        # Subscribed channels/markets for re-subscription on reconnect
        self._subscribed_markets: Dict[str, str] = {}
        self._subscribed_channels: Set[str] = {"price_change"}
        # Track raw token subscriptions
        self._active_tokens: Set[str] = set()

        # Background task handle
        self._listen_task: Optional[asyncio.Task] = None

        logger.info(
            "ws_client_initialized",
            url=ws_url,
            message="WebSocket client initialized",
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def seconds_since_last_message(self) -> float:
        return time.time() - self.last_message_received_at

    async def connect(self) -> bool:
        """Establish WebSocket connection."""
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            self._ws = await self._session.ws_connect(
                self._ws_url,
                headers=self._auth_headers,
                heartbeat=30.0,
                timeout=15.0,
            )

            self._connected = True
            self._reconnect_attempt = 0
            self.last_message_received_at = time.time()

            logger.info(
                "ws_connected",
                url=self._ws_url,
                message="WebSocket connected",
            )

            # Re-subscribe to all markets/channels
            await self._resubscribe()

            return True

        except Exception as e:
            self._connected = False
            logger.error(
                "ws_connect_error",
                error=str(e),
                attempt=self._reconnect_attempt,
                message=f"WebSocket connection failed: {e}",
            )
            return False

    async def disconnect(self) -> None:
        """Gracefully close WebSocket and session."""
        self._should_run = False

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session and not self._session.closed:
            await self._session.close()

        self._connected = False
    async def _push_subscription_state(self) -> None:
        """Push the batched array of active tokens to the WS in chunks to avoid INVALID OPERATION errors."""
        if not self.is_connected or not self._active_tokens:
            return

        # Filter out invalid tokens (None, empty strings, etc.)
        token_list = [str(t) for t in self._active_tokens if t]
        
        chunk_size = 50  # Polymarket prefers smaller batches
        
        for i in range(0, len(token_list), chunk_size):
            chunk = token_list[i : i + chunk_size]
            msg = {
                "assets_ids": chunk,
                "type": "market",
                "initial_dump": True
            }
            
            try:
                await self._ws.send_json(msg)
                logger.debug("ws_state_pushed", chunk_index=i//chunk_size, token_count=len(chunk))
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning("ws_state_push_error", error=str(e))

    async def subscribe_market(
        self, market_id: str, yes_token_id: str = None, no_token_id: str = None, condition_id: str = None, push_state: bool = True
    ) -> None:
        """Subscribe to updates for a specific market using token_id."""
        self._subscribed_markets[market_id] = condition_id if condition_id else market_id

        if yes_token_id:
            self.token_to_market[yes_token_id] = market_id
            self._active_tokens.add(yes_token_id)
        
        if no_token_id:
            self.token_to_market[no_token_id] = market_id
            self._active_tokens.add(no_token_id)
        
        if push_state:
            await self._push_subscription_state()

        logger.debug(
            "ws_subscribed",
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            message=f"Subscribed to market {market_id}",
        )

    async def unsubscribe_market(
        self, market_id: str, yes_token_id: str = None, push_state: bool = True
    ) -> None:
        """Unsubscribe from a market's updates and clear memory."""
        self._subscribed_markets.pop(market_id, market_id)
        if market_id in self.price_cache:
            del self.price_cache[market_id]
            
        if not yes_token_id:
            return

        self.token_to_market.pop(yes_token_id, None)
        self._active_tokens.discard(yes_token_id)
        
        logger.debug("ws_unsubscribed", market_id=market_id)
        
        if push_state:
            await self._push_subscription_state()

    async def _resubscribe(self) -> None:
        """Re-subscribe to all channels after reconnection."""
        await self._push_subscription_state()

        if self._active_tokens:
            logger.info(
                "ws_resubscribed",
                token_count=len(self._active_tokens),
                message=f"Resubscribed to {len(self._active_tokens)} tokens",
            )

    async def start(self) -> None:
        """
        Start the WebSocket listener as a background task.
        Messages populate self.price_cache dynamically.
        """
        self._should_run = True
        self._listen_task = asyncio.create_task(self._listen_loop())

        logger.info(
            "ws_listener_started",
            message="WebSocket listener task started",
        )

    async def _listen_loop(self) -> None:
        """
        Main listen loop with automatic reconnection.
        Runs as a separate asyncio.Task — never blocks the trading loop.
        """
        while self._should_run:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    wait = self._get_backoff_delay()
                    logger.warning(
                        "ws_reconnect_backoff",
                        wait_seconds=wait,
                        attempt=self._reconnect_attempt,
                        message=f"Reconnecting in {wait}s (attempt {self._reconnect_attempt})",
                    )
                    await asyncio.sleep(wait)
                    self._reconnect_attempt += 1
                    continue

            try:
                async for msg in self._ws:
                    if not self._should_run:
                        break

                    self.last_message_received_at = time.time()

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            
                            # Polymarket usually sends a list of events or a single event dict
                            # We normalize both to a list for iteration
                            events = data if isinstance(data, list) else [data]
                            
                            for d in events:
                                if not isinstance(d, dict):
                                    continue
                                    
                                token_id = d.get("asset_id", d.get("token_id"))
                                price = d.get("price")
                                if token_id and price is not None:
                                    # Cache by token_id (New correct way)
                                    self.price_cache[token_id] = float(price)

                                    # Also cache by market_id for backward compatibility (YES price only)
                                    market_id = self.token_to_market.get(token_id)
                                    if market_id:
                                        # Only map market_id to YES price to keep legacy logic working
                                        # This assumes the first token subscribed for a market is YES
                                        # or we check if it's the yes_token_id.
                                        # For now, token_id is the safe source of truth.
                                        pass 

                        except json.JSONDecodeError:
                            raw_data = msg.data[:200]
                            if "INVALID OPERATION" in raw_data:
                                logger.warning("ws_subscription_rejected", data=raw_data, message="Polymarket rejected the last subscription message (INVALID OPERATION)")
                            else:
                                logger.debug(
                                    "ws_invalid_json",
                                    data=raw_data,
                                    message="Invalid JSON from WebSocket (likely harmless status message)",
                                )

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(
                            "ws_error",
                            error=str(self._ws.exception()),
                            message="WebSocket error",
                        )
                        break

                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        logger.warning(
                            "ws_close_received",
                            message="WebSocket close frame received",
                        )
                        break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "ws_listen_error",
                    error=str(e),
                    message=f"WebSocket listen error: {e}",
                )

            # Connection lost
            self._connected = False
            if self._should_run:
                logger.warning(
                    "ws_disconnected_unexpected",
                    message="WebSocket disconnected — will reconnect",
                )

    def get_live_price(self, market_id: str) -> Optional[float]:
        """Get the latest price from the cache."""
        return self.price_cache.get(market_id)

    def _get_backoff_delay(self) -> float:
        """Calculate exponential backoff delay: 1→2→4→8→16→30 max."""
        delay = BACKOFF_BASE * (BACKOFF_FACTOR ** self._reconnect_attempt)
        return min(delay, BACKOFF_MAX)
