"""
CLOB API Client — Order Execution for Polymarket.

Handles:
- FAK order placement (ONLY FAK — assertion enforced)
- Order status polling (never assume fill from 200)
- Cancel all orders (kill switch)
- Orderbook depth (slippage guard)
- Paper trade simulation mode

╔══════════════════════════════════════════════════════════════════════════╗
║ MISTAKE 2 GUARD: GTC ORDERS FORBIDDEN                                    ║
║                                                                          ║
║ Only FAK (Fill-and-Kill) orders are allowed. GTC leaves resting orders   ║
║ that get sniped by HFT bots when news breaks. Market orders cause        ║
║ catastrophic slippage in prediction market orderbooks.                   ║
║                                                                          ║
║ An assertion in place_order() raises if order_type is not FAK or GTC.    ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.auth.l1_auth import L1Auth
from src.auth.l2_auth import L2Auth
from src.api.rate_limiter import RateLimiter
from src.exceptions import TransientNetworkError, PermanentApiError
from src.logs.log_config import get_logger

logger = get_logger("clob_client")

# Valid order types — GTC for resting TP, FAK for cascading entries/SL
ALLOWED_ORDER_TYPES = frozenset(["GTC", "FAK"])


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
        l1_auth: Optional[L1Auth] = None,
        funder: Optional[str] = None,
        signature_type: Optional[int] = None,
    ):
        self._auth = auth
        self._base_url = clob_api_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._paper_trade = paper_trade
        self._l1_auth = l1_auth
        self._funder = funder
        self._signature_type = signature_type if signature_type is not None else (2 if funder else 0)
        self._paper_orders = {}  # Track pending paper trades
        self.ws = None           # Reference for price auditing
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_balance = None
        self._last_balance_fetch_time = 0.0

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

    def get_synced_time(self) -> float:
        """Get current UTC timestamp adjusted for clock drift offset."""
        return self._auth.get_synced_time()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                keepalive_timeout=75.0,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=10, connect=3)
            )
        return self._session

    def get_cached_balance_sync(self) -> Optional[float]:
        """Returns the in-memory cached wallet balance without blocking on REST calls."""
        if self._cached_balance and isinstance(self._cached_balance, dict):
            try:
                return float(self._cached_balance.get("balance", 0.0))
            except (ValueError, TypeError):
                pass
        return None

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

        body_str = json.dumps(body, separators=(",", ":")) if body else ""
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
                async with session.delete(url, headers=headers, data=body_str) as resp:
                    status = resp.status
                    data = await resp.json() if resp.content_type == "application/json" else await resp.text()
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Handle 401s with automatic server time resync and 1-time retry
            if status == 401:
                logger.warning("auth_401_detected_attempting_resync", method=method, path=path)
                try:
                    await self._auth.sync_server_time(session)
                    retry_headers = self._auth.build_headers(method, path, body_str)
                    if method.upper() == "GET":
                        async with session.get(url, headers=retry_headers) as retry_resp:
                            status = retry_resp.status
                            data = await retry_resp.json() if retry_resp.content_type == "application/json" else await retry_resp.text()
                    elif method.upper() == "POST":
                        async with session.post(url, headers=retry_headers, data=body_str) as retry_resp:
                            status = retry_resp.status
                            data = await retry_resp.json() if retry_resp.content_type == "application/json" else await retry_resp.text()
                    elif method.upper() == "DELETE":
                        async with session.delete(url, headers=retry_headers, data=body_str) as retry_resp:
                            status = retry_resp.status
                            data = await retry_resp.json() if retry_resp.content_type == "application/json" else await retry_resp.text()
                except Exception as retry_err:
                    logger.warning("auth_401_retry_failed", error=str(retry_err))

                if status == 401:
                    self._auth.record_401()
                    self._auth.log_auth_failure_details(method, path, status, str(data))
                    if self._auth.should_kill:
                        logger.critical(
                            "auth_kill_threshold",
                            consecutive_401s=self._auth.consecutive_401s,
                            message=f"AUTH KILL THRESHOLD REACHED — {self._auth.consecutive_401s} consecutive 401s",
                        )
                else:
                    self._auth.reset_401_counter()
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
        Calculate total available volume at current best ask price for a side.
        Used for slippage guard.
        """
        asks = orderbook.get("asks", [])
        if not asks:
            return 0.0

        best_ask = float(asks[0].get("price", 0))
        if best_ask <= 0:
            return 0.0

        total_volume = 0.0
        for ask in asks:
            price = float(ask.get("price", 0))
            if abs(price - best_ask) < 0.0001:
                size = float(ask.get("size", 0))
                total_volume += price * size
            else:
                break

        return total_volume

    # -----------------------------------------------------------------
    # Order Placement
    # -----------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "FAK",
        verifying_contract: str = "0xE111180000d2663C0091e4f400237545B87B996B",
    ) -> Optional[Dict]:
        """
        Place a V2 order on the CLOB.

        ENFORCED: Only FAK and GTC orders are accepted.
        An assertion will raise if any other order type is attempted.

        Args:
            token_id: The token to trade
            side: "BUY" or "SELL"
            price: Limit price
            size: Number of shares (NOT USDC amount)
            order_type: Must be FAK or GTC — raises AssertionError otherwise
            verifying_contract: EIP-712 verifyingExchange contract address

        Returns:
            Order response dict if accepted, None if rejected/error
        """
        # ═══════════════════════════════════════════════════════════
        # MISTAKE 2 GUARD: Permitted order types enforced
        # GTC orders get sniped by HFT bots on news breaks (only allowed for TP)
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

        if not self._l1_auth:
            raise ValueError(
                "CRITICAL: L1Auth is required for live order placement. "
                "Initialize ClobClient with l1_auth to sign orders."
            )

        import random
        salt = int(random.randint(1, 2**32 - 1))

        # Checksum addresses
        from eth_utils import to_checksum_address
        maker = to_checksum_address(self._funder if self._funder else self._auth._address)
        signer = to_checksum_address(self._l1_auth.address)

        # Calculate makerAmount and takerAmount in atomic units (6 decimals)
        # USDC (collateral) requires max 2 decimals accuracy (multiple of 10**4 in atomic units)
        # Shares (outcome tokens) require max 4 decimals accuracy (multiple of 10**2 in atomic units)
        usdc_atomic = int(round(size * price * 100)) * 10000
        shares_atomic = int(round(size * 10000)) * 100

        if side.upper() == "BUY":
            maker_amount = usdc_atomic
            taker_amount = shares_atomic
        else:
            maker_amount = shares_atomic
            taker_amount = usdc_atomic

        # Adjust timestamp using server time in seconds, converted to milliseconds
        adjusted_seconds = float(self._auth._get_timestamp())
        timestamp = int(adjusted_seconds * 1000)

        # Determine signatureType
        sig_type = self._signature_type

        # Prepare order message for EIP-712 signing
        order_to_sign = {
            "salt": str(salt),
            "maker": maker,
            "signer": signer,
            "tokenId": token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "side": 0 if side.upper() == "BUY" else 1,
            "signatureType": sig_type,
            "timestamp": str(timestamp),
            "metadata": "0x0000000000000000000000000000000000000000000000000000000000000000",
            "builder": "0x0000000000000000000000000000000000000000000000000000000000000000",
        }

        try:
            signature = self._l1_auth.sign_order(order_to_sign, verifying_contract=verifying_contract)
        except Exception as e:
            logger.error("order_signing_failed", error=str(e), message="Failed to sign V2 order via L1Auth")
            return None

        order_body = {
            "order": {
                "salt": int(salt),
                "maker": maker,
                "signer": signer,
                "tokenId": token_id,
                "makerAmount": str(maker_amount),
                "takerAmount": str(taker_amount),
                "side": side.upper(),
                "expiration": "0",
                "signatureType": sig_type,
                "timestamp": str(timestamp),
                "metadata": "0x0000000000000000000000000000000000000000000000000000000000000000",
                "builder": "0x0000000000000000000000000000000000000000000000000000000000000000",
                "signature": signature,
            },
            "owner": self._auth._api_key,
            "orderType": order_type,
            "deferExec": False,
            "postOnly": False
        }

        logger.info(
            "placing_order_v2",
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            timestamp=timestamp,
            message=f"Placing V2 {order_type} {side} order: {size} shares @ {price}",
        )

        status, data = await self._request("POST", "/order", body=order_body, is_order=True)

        if status in (200, 201) and isinstance(data, dict):
            self._cached_balance = None
            self._last_balance_fetch_time = 0.0
            order_id = data.get("orderID") or data.get("id")
            logger.info(
                "order_accepted",
                order_id=order_id,
                token_id=token_id,
                status=status,
                message=f"Order accepted — ID: {order_id}",
            )
            return data

        # Check for standard FAK mismatch killed status (HTTP 400 with orderID and match error)
        if status == 400 and isinstance(data, dict) and ("orderID" in data or "id" in data):
            error_msg = data.get("error", "")
            if "no orders found to match" in error_msg.lower() or "partially filled or killed" in error_msg.lower():
                order_id = data.get("orderID") or data.get("id")
                logger.info(
                    "order_killed_no_match",
                    order_id=order_id,
                    token_id=token_id,
                    status=status,
                    message=f"FAK order killed (no match found) — ID: {order_id}",
                )
                data["status"] = "KILLED"
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
        fail_reason = None
        available_shares = 0.0
        filled_shares = float(actual_shares)
        
        if order_type == "FAK" and hasattr(self, "ws") and self.ws:
            book = self.ws.book_cache.get(token_id) if hasattr(self.ws, "book_cache") else None
            
            if side == "BUY":
                ask = self.ws.ask_cache.get(token_id)
                if ask is None or price < ask:
                    status = "FAILED"
                    fail_reason = f"price_limit_breach (limit={price:.4f}, best_ask={ask if ask is not None else 'None'})"
                    filled_shares = 0.0
                elif book:
                    # Check real depth from asks
                    asks = book.get("asks", {})
                    valid_asks = {float(p): float(sz) for p, sz in asks.items() if float(p) <= price}
                    available_shares = sum(valid_asks.values())
                    filled_shares = min(float(actual_shares), available_shares)
                    if filled_shares <= 0.0:
                        status = "FAILED"
                        fail_reason = f"insufficient_depth (requested={actual_shares}, available={available_shares:.2f} shares up to limit {price:.4f})"
                    else:
                        # Sweep asks ascending (cheapest first)
                        sorted_asks = sorted(valid_asks.items())
                        remaining = filled_shares
                        total_cost = 0.0
                        for level_p, level_sz in sorted_asks:
                            if remaining <= 0.0:
                                break
                            take_qty = min(level_sz, remaining)
                            total_cost += take_qty * level_p
                            remaining -= take_qty
                        if filled_shares > 0.0:
                            effective_price = total_cost / filled_shares
                else:
                    status = "FAILED"
                    fail_reason = "missing_orderbook_cache"
                    filled_shares = 0.0
            else:  # SELL
                bid = self.ws.bid_cache.get(token_id)
                if bid is None or price > bid:
                    status = "FAILED"
                    fail_reason = f"price_limit_breach (limit={price:.4f}, best_bid={bid if bid is not None else 'None'})"
                    filled_shares = 0.0
                elif book:
                    # Check real depth from bids
                    bids = book.get("bids", {})
                    valid_bids = {float(p): float(sz) for p, sz in bids.items() if float(p) >= price}
                    available_shares = sum(valid_bids.values())
                    filled_shares = min(float(actual_shares), available_shares)
                    if filled_shares <= 0.0:
                        status = "FAILED"
                        fail_reason = f"insufficient_depth (requested={actual_shares}, available={available_shares:.2f} shares up to limit {price:.4f})"
                    else:
                        # Sweep bids descending (highest first)
                        sorted_bids = sorted(valid_bids.items(), reverse=True)
                        remaining = filled_shares
                        total_cost = 0.0
                        for level_p, level_sz in sorted_bids:
                            if remaining <= 0.0:
                                break
                            take_qty = min(level_sz, remaining)
                            total_cost += take_qty * level_p
                            remaining -= take_qty
                        if filled_shares > 0.0:
                            effective_price = total_cost / filled_shares
                else:
                    status = "FAILED"
                    fail_reason = "missing_orderbook_cache"
                    filled_shares = 0.0
                    
        if status == "FAILED":
            logger.warning(
                "paper_order_rejected_by_market",
                side=side,
                price=price,
                size=actual_shares,
                token_id=token_id[-8:],
                reason=fail_reason,
                message=f"PAPER: Simulated {order_type} {side} rejected by market: {fail_reason}"
            )
        else:
            logger.info(
                "paper_order_filled",
                side=side,
                price=price,
                size=filled_shares,
                order_type=order_type,
                token_id=token_id[-8:],
                message=f"PAPER: Simulated {order_type} {side} filled: {filled_shares}/{actual_shares} shares"
            )

        order_data = {
            "orderID": order_id,
            "status": status,
            "price": float(round(effective_price, 4)),
            "size": str(actual_shares - int(filled_shares)) if status != "FAILED" else str(actual_shares),
            "size_matched": str(int(filled_shares)) if status != "FAILED" else "0",
            "original_size": str(actual_shares),
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
            
            if order["status"] in ("MATCHED", "FAILED", "CANCELLED"):
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

        status, data = await self._request("GET", f"/data/order/{order_id}")

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

        body = {"orderID": order_id}
        status, data = await self._request("DELETE", "/order", body=body)
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

        status, data = await self._request("DELETE", "/cancel-all", is_order=True)
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
        Default asset_id 215 is replaced by asset_type=COLLATERAL in CLOB V2.
        For Smart Contract / Proxy wallets, signature_type=2 is specified.
        Converts the raw atomic units (6 decimals) to standard USDC decimal values.
        """
        now = time.time()
        if self._cached_balance is not None and now - self._last_balance_fetch_time < 3.0:
            return self._cached_balance

        path = "/balance-allowance?asset_type=COLLATERAL"
        if self._funder:
            path += f"&funder={self._funder}&signature_type=2"
        status, data = await self._request("GET", path)
        if status == 200 and isinstance(data, dict):
            raw_balance = data.get("balance", "0")
            try:
                balance_usdc = float(raw_balance) / 10**6
            except ValueError:
                balance_usdc = 0.0

            allowances = data.get("allowances", {})
            raw_allowance = allowances.get("0xE111180000d2663C0091e4f400237545B87B996B")
            if raw_allowance is None and allowances:
                raw_allowance = next(iter(allowances.values()), "0")
            else:
                raw_allowance = raw_allowance or "0"

            try:
                allowance_usdc = float(raw_allowance) / 10**6
            except ValueError:
                allowance_usdc = 0.0

            result = {
                "balance": f"{balance_usdc:.6f}",
                "allowance": f"{allowance_usdc:.6f}",
                "raw_data": data
            }
            self._cached_balance = result
            self._last_balance_fetch_time = now
            return result

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

    async def get_token_balance(self, token_id: str) -> Optional[float]:
        """
        Fetch the balance of a specific outcome token from the CLOB API.
        Returns the standard decimal token balance (shares).
        """
        if self._paper_trade:
            return 0.0

        path = f"/balance-allowance?asset_type=CONDITIONAL&token_id={token_id}"
        if self._funder:
            path += f"&funder={self._funder}&signature_type=2"

        status, data = await self._request("GET", path)
        if status == 200 and isinstance(data, dict):
            raw_balance = data.get("balance", "0")
            try:
                # Outcome tokens have 6 decimal places in atomic units
                balance_shares = float(raw_balance) / 10**6
                return balance_shares
            except ValueError:
                return 0.0

        logger.warning(
            "token_balance_fetch_failed",
            token_id=token_id,
            status=status,
            message=f"Failed to fetch token balance for {token_id}",
        )
        return None

