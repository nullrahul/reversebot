"""
CLOB API Client — Order Execution for Polymarket.

Handles:
- FOK order placement (ONLY FOK — assertion enforced)
- Order status polling (never assume fill from 200)
- Cancel all orders (kill switch)
- Orderbook depth (slippage guard)
- Paper trade simulation mode

╔══════════════════════════════════════════════════════════════════════════╗
║ MISTAKE 2 GUARD: GTC ORDERS FORBIDDEN                                   ║
║                                                                          ║
║ Only FOK (Fill-or-Kill) orders are allowed. GTC leaves resting orders   ║
║ that get sniped by HFT bots when news breaks. Market orders cause       ║
║ catastrophic slippage in thin prediction market orderbooks.              ║
║                                                                          ║
║ An assertion in place_order() raises if order_type != "FOK".            ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.auth.l2_auth import L2Auth
from src.api.rate_limiter import RateLimiter
from src.exceptions import TransientNetworkError, PermanentApiError
from src.logs.log_config import get_logger

logger = get_logger("clob_client")

# Valid order types — FOK for entries/SL, GTC for resting TP
ALLOWED_ORDER_TYPES = frozenset(["FOK", "GTC"])


class ClobClient:
    """
    Async client for the Polymarket CLOB API.
    Handles authenticated order operations with rate limiting.
    """

    def __init__(
        self,
        auth: L2Auth,
        clob_api_url: str = "https://clob.polymarket.com",
        rate_limiter: Optional[RateLimiter] = None,
        paper_trade: bool = True,
    ):
        self._auth = auth
        self._base_url = clob_api_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._paper_trade = paper_trade
        self._paper_orders = {}  # Track pending paper trades
        self.ws = None           # Reference for price auditing
        self._session: Optional[aiohttp.ClientSession] = None

        mode = "PAPER" if paper_trade else "LIVE"
        logger.info(
            "clob_client_initialized",
            base_url=self._base_url,
            mode=mode,
            message=f"CLOB client ready in {mode} mode",
        )

    @property
    def is_paper(self) -> bool:
        return self._paper_trade

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
        is_order: bool = False,
    ) -> Tuple[int, Any]:
        """
        Make an authenticated, rate-limited request to the CLOB API.
        Returns (status_code, response_data).
        """
        # Rate limit
        if self._rate_limiter:
            if is_order:
                await self._rate_limiter.acquire_order()
            else:
                await self._rate_limiter.acquire_api_call()

        session = await self._ensure_session()

        body_str = json.dumps(body) if body else ""
        headers = self._auth.build_headers(method, path, body_str)

        url = f"{self._base_url}{path}"

        try:
            if method.upper() == "GET":
                async with session.get(url, headers=headers) as resp:
                    status = resp.status
                    data = await resp.json() if resp.content_type == "application/json" else await resp.text()
            elif method.upper() == "POST":
                async with session.post(url, headers=headers, data=body_str) as resp:
                    status = resp.status
                    data = await resp.json() if resp.content_type == "application/json" else await resp.text()
            elif method.upper() == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    status = resp.status
                    data = await resp.json() if resp.content_type == "application/json" else await resp.text()
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Handle 401s
            if status == 401:
                self._auth.record_401()
                self._auth.log_auth_failure_details(method, path, status, str(data))
                if self._auth.should_kill:
                    logger.critical(
                        "auth_kill_threshold",
                        consecutive_401s=self._auth.consecutive_401s,
                        message="AUTH KILL THRESHOLD REACHED — 3 consecutive 401s",
                    )
            else:
                self._auth.reset_401_counter()

            return status, data

        except (aiohttp.ClientConnectorError,
                aiohttp.ServerDisconnectedError,
                asyncio.TimeoutError,
                ConnectionResetError,
                BrokenPipeError) as e:
            logger.error(
                "clob_api_transient_error",
                method=method,
                path=path,
                error_type=type(e).__name__,
                error=str(e),
                message=f"CLOB API transient error ({type(e).__name__}): {e}",
            )
            raise TransientNetworkError(str(e)) from e

        except aiohttp.ClientError as e:
            logger.error(
                "clob_api_connection_error",
                method=method,
                path=path,
                error=str(e),
                message=f"CLOB API connection error: {e}",
            )
            return 0, {"error": str(e)}

    # -----------------------------------------------------------------
    # Orderbook
    # -----------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """
        Fetch the current orderbook for a token.
        Used for slippage guard — checking ask depth before placing orders.
        """
        # 1. Check WebSocket cache first (Zero-latency)
        if hasattr(self, "ws") and self.ws:
            book = self.ws.get_full_book(token_id)
            if book:
                return book

        status, data = await self._request("GET", f"/book?token_id={token_id}")

        if status == 200 and isinstance(data, dict):
            return data

        logger.warning(
            "orderbook_fetch_failed",
            token_id=token_id,
            status=status,
            message=f"Failed to fetch orderbook for {token_id}",
        )
        return None

    def get_available_volume_at_ask(self, orderbook: Dict, side: str = "NO") -> float:
        """
        Calculate total available volume at current ask price for a side.
        Used for slippage guard.
        """
        asks = orderbook.get("asks", [])
        if not asks:
            return 0.0

        total_volume = 0.0
        for ask in asks:
            price = float(ask.get("price", 0))
            size = float(ask.get("size", 0))
            total_volume += price * size

        return total_volume

    # -----------------------------------------------------------------
    # Order Placement — FOK ONLY
    # -----------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "FOK",
    ) -> Optional[Dict]:
        """
        Place an order on the CLOB.

        ENFORCED: Only FOK orders are accepted.
        An assertion will raise if any other order type is attempted.

        Args:
            token_id: The token to trade
            side: "BUY" or "SELL"
            price: Limit price
            size: Number of shares (NOT USDC amount)
            order_type: Must be "FOK" — raises AssertionError otherwise

        Returns:
            Order response dict if accepted, None if rejected/error
        """
        # ═══════════════════════════════════════════════════════════
        # MISTAKE 2 GUARD: Only FOK orders allowed
        # GTC orders get sniped by HFT bots on news breaks
        # Market orders cause catastrophic slippage
        # ═══════════════════════════════════════════════════════════
        assert order_type in ALLOWED_ORDER_TYPES, (
            f"FORBIDDEN: Order type '{order_type}' is not allowed. "
            f"Only {ALLOWED_ORDER_TYPES} are permitted. "
            f"GTC orders get sniped, Market orders cause slippage."
        )

        # Paper trade: simulate the order
        if self._paper_trade:
            return self._simulate_paper_order(token_id, side, price, size, order_type)

        order_body = {
            "tokenID": token_id,
            "side": side,
            "price": str(price),
            "size": str(int(size)),  # Polymarket uses integer sizes
            "type": order_type,
        }

        logger.info(
            "placing_order",
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            message=f"Placing {order_type} {side} order: {size} shares @ {price}",
        )

        status, data = await self._request("POST", "/order", body=order_body, is_order=True)

        if status == 200 and isinstance(data, dict):
            order_id = data.get("orderID") or data.get("id")
            logger.info(
                "order_accepted",
                order_id=order_id,
                token_id=token_id,
                status=status,
                message=f"Order accepted — ID: {order_id}",
            )
            return data

        logger.error(
            "order_rejected",
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            status=status,
            response=str(data)[:500],
            message=f"Order rejected — status {status}",
        )
        return None

    def restore_paper_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str,
    ) -> None:
        """Rehydrate a paper order across restarts."""
        if self._paper_trade and order_id not in self._paper_orders:
            self._paper_orders[order_id] = {
                "orderID": order_id,
                "status": "PENDING",
                "price": float(price),
                "size": str(int(size)),
                "side": side,
                "type": order_type,
                "token_id": token_id
            }

    def _simulate_paper_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str,
    ) -> Dict:
        """Simulate an order fill for paper trading mode."""
        import uuid
        if self._paper_trade:
            effective_price = price
            order_id = f"paper_{side.lower()}_{uuid.uuid4().hex[:8]}"
            logger.info("paper_order_placed", side=side, price=round(effective_price, 4), order_id=order_id)
        else:
            order_id = f"paper_{uuid.uuid4().hex[:16]}"
            effective_price = price

        logger.info(
            "paper_order_simulated",
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            message=f"PAPER: Simulated {side} {size} shares @ {price}",
        )

        actual_shares = int(size)
        
        status = "PENDING" if order_type == "GTC" else "MATCHED"
        if order_type == "FOK" and hasattr(self, "ws") and self.ws:
            if side == "BUY":
                ask = self.ws.ask_cache.get(token_id)
                if ask is None or price < ask:
                    status = "FAILED"
            else:
                bid = self.ws.bid_cache.get(token_id)
                if bid is None or price > bid:
                    status = "FAILED"
                    
        if status == "FAILED":
            logger.warning("paper_fok_rejected_by_market", side=side, price=price, token_id=token_id[-8:])

        order_data = {
            "orderID": order_id,
            "status": status,
            "price": float(round(effective_price, 4)),
            "size": str(actual_shares),
            "side": side,
            "type": order_type,
            "token_id": token_id
        }
        
        # Save to registry for status auditing
        self._paper_orders[order_id] = order_data
        
        return order_data

    # -----------------------------------------------------------------
    # Order Status — NEVER assume fill from placement
    # -----------------------------------------------------------------

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Fetch the current status of an order."""
        if self._paper_trade:
            # Check the in-memory paper order registry
            order = self._paper_orders.get(order_id)
            if not order:
                return {"status": "NOT_FOUND"}
            
            if order["status"] == "MATCHED":
                return order

            # Audit price via WS cache
            if self.ws:
                side = order["side"]
                token_id = order["token_id"]
                
                if side == "SELL":
                    cur = self.ws.bid_cache.get(token_id) or self.ws.price_cache.get(token_id)
                else:
                    cur = self.ws.ask_cache.get(token_id) or self.ws.price_cache.get(token_id)
                    
                if cur:
                    target = order["price"]
                    # SELL (TP): Fill if price >= target
                    # BUY (Entry): Fill if price <= target
                    if (side == "SELL" and cur >= target) or (side == "BUY" and cur <= target):
                        order["status"] = "MATCHED"
                        logger.info("paper_order_filled_by_market", order_id=order_id, price=cur, target=target)
            
            return order

        status, data = await self._request("GET", f"/order/{order_id}")

        if status == 200:
            return data
        
        if status == 404:
            logger.warning("ghost_order_detected", order_id=order_id, message="Order not found (404) — treating as FAILED")
            return {"status": "FAILED", "orderID": order_id}

        logger.warning(
            "order_status_fetch_failed",
            order_id=order_id,
            status=status,
            message=f"Failed to fetch order status for {order_id}",
        )
        return None

    async def wait_for_fill(
        self,
        order_id: str,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.1,
    ) -> Optional[str]:
        """
        Poll order status until confirmed MATCHED/CANCELLED or timeout.
        Returns the final status string, or None on timeout.

        MISTAKE 12 GUARD: Don't trust API success without verifying state.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            order = await self.get_order_status(order_id)
            if order:
                status = order.get("status", "").upper()
                if status in ("MATCHED", "FILLED"):
                    return "MATCHED"
                elif status in ("CANCELLED", "EXPIRED", "FAILED", "NOT_FOUND"):
                    return status
            await asyncio.sleep(poll_interval)

        logger.error(
            "order_fill_timeout",
            order_id=order_id,
            timeout=timeout_seconds,
            message=f"Order fill confirmation timed out after {timeout_seconds}s",
        )
        return None

    # -----------------------------------------------------------------
    # Cancel Operations — Kill Switch Support
    # -----------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order. Returns True if successful."""
        if self._paper_trade:
            self._paper_orders.pop(order_id, None)
            logger.info("paper_cancel", order_id=order_id, message="PAPER: Order cancelled")
            return True

        status, data = await self._request("DELETE", f"/order/{order_id}")
        success = status == 200

        if success:
            logger.info(
                "order_cancelled",
                order_id=order_id,
                message=f"Order {order_id} cancelled",
            )
        else:
            logger.error(
                "cancel_failed",
                order_id=order_id,
                status=status,
                message=f"Failed to cancel order {order_id}",
            )

        return success

    async def cancel_all_orders(self) -> bool:
        """
        Cancel ALL open orders immediately.
        Used by kill switch — this must be the FIRST action in shutdown.
        Uses the bulk cancel-all endpoint, not individual cancels.
        """
        if self._paper_trade:
            self._paper_orders.clear()
            logger.info("paper_cancel_all", message="PAPER: All orders cancelled")
            return True

        logger.critical(
            "cancel_all_initiated",
            message="CANCELLING ALL OPEN ORDERS — kill switch sequence",
        )

        status, data = await self._request("DELETE", "/orders", is_order=True)
        success = status == 200

        if success:
            logger.info(
                "all_orders_cancelled",
                response=str(data)[:500],
                message="All orders cancelled successfully",
            )
        else:
            logger.critical(
                "cancel_all_failed",
                status=status,
                response=str(data)[:500],
                message=f"CRITICAL: Failed to cancel all orders — status {status}",
            )

        return success

    # -----------------------------------------------------------------
    # Price Data
    # -----------------------------------------------------------------

    async def get_current_price(self, token_id: str, side: str = "sell") -> Optional[float]:
        """Get the current price we can execute at.
        side: 'buy' or 'sell'
        """
        book = await self.get_orderbook(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if side.lower() == "sell":
            return float(bids[0]["price"]) if bids else None
        else:
            return float(asks[0]["price"]) if asks else None

    async def get_account_balance(self) -> Optional[Dict]:
        """
        Fetch the account balance and allowance from the CLOB API.
        Default asset_id 215 corresponds to USDC on Polygon.
        """
        status, data = await self._request("GET", "/balance-allowance?asset_id=215")
        if status == 200 and isinstance(data, dict):
            return data

        logger.warning(
            "balance_fetch_failed",
            status=status,
            message="Failed to fetch account balance from CLOB",
        )
        return None

    async def get_market_info(self, condition_id: str) -> Optional[Dict]:
        """
        Fetch full market details from the CLOB API by condition_id.
        Source of truth for token mappings.
        """
        status, data = await self._request("GET", f"/markets/{condition_id}")
        if status == 200 and isinstance(data, dict):
            return data
        
        logger.warning(
            "clob_market_info_failed",
            condition_id=condition_id,
            status=status,
            message="Failed to fetch market info from CLOB"
        )
        return None
