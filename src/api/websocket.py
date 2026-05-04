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

from src.exceptions import TransientNetworkError, WebSocketDroppedError

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

        # In-memory L2 Orderbook Replica
        # Structure: self.book_cache[token_id] = {"bids": {price: size}, "asks": {price: size}}
        self.book_cache: Dict[str, Dict[str, Dict[float, float]]] = {}
        
        self.price_cache: Dict[str, float] = {}
        self.bid_cache: Dict[str, float] = {}
        self.ask_cache: Dict[str, float] = {}
        self.token_to_market: Dict[str, str] = {}
        
        # Health tracking
        self.last_message_received_at: float = time.time()

        # Connection state
        self._connected = False
        self._should_run = False
        self._reconnect_attempt = 0

        # Subscribed channels/markets for re-subscription on reconnect
        self._subscribed_markets: Dict[str, str] = {}
        self._subscribed_channels: List[str] = ["price_change", "book", "user"]
        # Track raw token subscriptions
        self._active_tokens: Set[str] = set()

        # User event queue (MINED, etc.)
        self.user_queue: asyncio.Queue = asyncio.Queue()

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
            
            # Clear caches to ensure no stale data is used
            self.price_cache.clear()
            self.bid_cache.clear()
            self.ask_cache.clear()

            logger.info(
                "ws_connected",
                url=self._ws_url,
                message="WebSocket connected",
            )

            # Re-subscribe to all markets/channels
            await self._resubscribe()

            return True

        except (ConnectionResetError, BrokenPipeError) as e:
            self._connected = False
            logger.error(
                "ws_connection_dropped",
                error_type=type(e).__name__,
                error=str(e),
                message=f"WebSocket pipe broken ({type(e).__name__}) — clearing price cache",
            )
            # Clear all cached prices to prevent stale data
            self.price_cache.clear()
            self.bid_cache.clear()
            self.ask_cache.clear()
            return False

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self._connected = False
            logger.error(
                "ws_connect_error",
                error=str(e),
                error_type=type(e).__name__,
                attempt=self._reconnect_attempt,
                message=f"WebSocket connection failed ({type(e).__name__}): {e}",
            )
            return False

        except Exception as e:
            self._connected = False
            logger.error(
                "ws_connect_unexpected",
                error=str(e),
                error_type=type(e).__name__,
                attempt=self._reconnect_attempt,
                message=f"Unexpected WS connect error: {e}",
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

        # Filter out invalid tokens
        token_list = []
        for t in self._active_tokens:
            if t:
                token_list.append(str(t))
        
        chunk_size = 50  # Polymarket prefers smaller batches
        
        for i in range(0, len(token_list), chunk_size):
            chunk = token_list[i : i + chunk_size]
            msg = {
                "operation": "subscribe",
                "assets_ids": chunk,
                "custom_feature_enabled": True
            }
            
            try:
                await self._ws.send_json(msg)
                logger.debug("ws_subscription_pushed", chunk_index=i//chunk_size, token_count=len(chunk), channels=self._subscribed_channels)
                logger.debug(
                    "ws_batch_subscribed", 
                    chunk_index=i//chunk_size, 
                    count=len(chunk),
                    first_id=chunk[0][-8:],
                    last_id=chunk[-1][-8:],
                    message=f"Subscribed to batch {i//chunk_size} ({len(chunk)} tokens)"
                )
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
        self, market_id: str, yes_token_id: str = None, no_token_id: str = None, push_state: bool = True
    ) -> None:
        """Unsubscribe from a market's updates and clear memory."""
        self._subscribed_markets.pop(market_id, market_id)
        if market_id in self.price_cache:
            del self.price_cache[market_id]
        if market_id in self.bid_cache:
            del self.bid_cache[market_id]
        if market_id in self.ask_cache:
            del self.ask_cache[market_id]

        if yes_token_id:
            self.token_to_market.pop(yes_token_id, None)
            self._active_tokens.discard(yes_token_id)

        if no_token_id:
            self.token_to_market.pop(no_token_id, None)
            self._active_tokens.discard(no_token_id)

        logger.debug("ws_unsubscribed", market_id=market_id)

        if push_state:
            await self._push_subscription_state()

    async def _resubscribe(self) -> None:
        """Re-subscribe to all channels after reconnection."""
        await self._push_subscription_state()
        
        # Subscribe to authenticated user channel
        if self.is_connected:
            try:
                await self._ws.send_json({"operation": "subscribe", "type": "user"})
                logger.info("ws_user_channel_subscribed")
            except Exception as e:
                logger.warning("ws_user_sub_failed", error=str(e))

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
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(
            "ws_listener_started",
            message="WebSocket listener and heartbeat tasks started",
        )

    async def _heartbeat_loop(self) -> None:
        """Keep the connection alive and ensure fresh prices as per architectural blueprint."""
        while self._should_run:
            if self.is_connected:
                try:
                    # Injects "PING" every 10s to prevent stale false prices
                    await self._ws.send_str("PING")
                    logger.debug("ws_heartbeat_sent")
                except Exception:
                    pass
            await asyncio.sleep(10)

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
                                    
                                token_id = str(d.get("asset_id") or d.get("token_id") or d.get("market_id") or "")
                                
                                # STRICT GUARD: Ignore short market_ids to prevent memory poisoning
                                if len(token_id) < 50:
                                    continue
                                    
                                event_type = d.get("event_type") or d.get("type") or d.get("event")
                                price = d.get("price")
                                
                                if token_id and price is not None:
                                    # Cache by token_id (Trade/Price Change)
                                    self.price_cache[token_id] = float(price)
                                
                                elif token_id and event_type == "price_change":
                                    # L2 DELTA PATH: Precise orderbook replica update
                                    side = d.get("side") # "BUY" (bid) or "SELL" (ask)
                                    price = float(d.get("price") or 0)
                                    size = float(d.get("size") or 0)
                                    
                                    if token_id not in self.book_cache:
                                        self.book_cache[token_id] = {"bids": {}, "asks": {}}
                                    
                                    book_side = "bids" if side == "BUY" else "asks"
                                    
                                    if size == 0:
                                        # DELETION RULE: Size 0 means level is depleted
                                        self.book_cache[token_id][book_side].pop(price, None)
                                    else:
                                        self.book_cache[token_id][book_side][price] = size
                                    
                                    self._update_best_bid_ask(token_id)

                                elif token_id and event_type == "book":
                                    # L2 SNAPSHOT PATH: Reset replica for this token
                                    self.book_cache[token_id] = {"bids": {}, "asks": {}}
                                    
                                    for b in d.get("bids", []):
                                        self.book_cache[token_id]["bids"][float(b["price"])] = float(b["size"])
                                    for a in d.get("asks", []):
                                        self.book_cache[token_id]["asks"][float(a["price"])] = float(a["size"])
                                    
                                    self._update_best_bid_ask(token_id)

                                elif token_id and event_type == "best_bid_ask":
                                    # FAST PATH: Top of the book changed (fallback/redundancy)
                                    bid = d.get("best_bid")
                                    ask = d.get("best_ask")
                                    
                                    if bid is not None:
                                        self.bid_cache[token_id] = float(bid)
                                        self.price_cache[token_id] = float(bid) 
                                    
                                    if ask is not None:
                                        self.ask_cache[token_id] = float(ask)
                                
                                elif event_type == "mined":
                                    # AUTHENTICATED USER PATH: Order confirmed on-chain
                                    logger.info("ws_order_mined", order_id=d.get("order_id"), message="Order successfully mined on Polygon")
                                    await self.user_queue.put(d)

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
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(
                    "ws_pipe_broken",
                    error_type=type(e).__name__,
                    error=str(e),
                    message=f"WebSocket pipe broken ({type(e).__name__}) — clearing cache and reconnecting",
                )
                # Clear all cached prices to prevent stale data serving
                self.price_cache.clear()
                self.bid_cache.clear()
                self.ask_cache.clear()
            except Exception as e:
                logger.error(
                    "ws_listen_error",
                    error=str(e),
                    error_type=type(e).__name__,
                    message=f"WebSocket listen error ({type(e).__name__}): {e}",
                )

            # Connection lost
            self._connected = False
            if self._should_run:
                logger.warning(
                    "ws_disconnected_unexpected",
                    message="WebSocket disconnected — will reconnect",
                )

    def _update_best_bid_ask(self, token_id: str) -> None:
        """Update bid/ask/price caches from the L2 book replica."""
        book = self.book_cache.get(token_id)
        if not book:
            return

        bids = book.get("bids", {})
        asks = book.get("asks", {})

        if bids:
            best_bid = max(bids.keys())
            self.bid_cache[token_id] = best_bid
            self.price_cache[token_id] = best_bid
        
        if asks:
            best_ask = min(asks.keys())
            self.ask_cache[token_id] = best_ask

    def get_live_price(self, token_id: str) -> Optional[float]:
        """Get the latest price from the cache."""
        return self.price_cache.get(token_id)

    def get_full_book(self, token_id: str) -> Optional[Dict]:
        """Returns a snapshot-compatible dict of the local L2 replica."""
        book = self.book_cache.get(token_id)
        if not book:
            return None
        
        # Sort for compatibility with existing book consumers
        sorted_bids = [{"price": str(p), "size": str(s)} for p, s in sorted(book["bids"].items(), reverse=True)]
        sorted_asks = [{"price": str(p), "size": str(s)} for p, s in sorted(book["asks"].items())]
        
        return {
            "token_id": token_id,
            "bids": sorted_bids,
            "asks": sorted_asks
        }

    def _get_backoff_delay(self) -> float:
        """Calculate exponential backoff delay: 1→2→4→8→16→30 max."""
        delay = BACKOFF_BASE * (BACKOFF_FACTOR ** self._reconnect_attempt)
        return min(delay, BACKOFF_MAX)
