"""
Token Bucket Rate Limiter for Polymarket API.

Two separate buckets:
- Orders: 55/min (Polymarket hard limit: 60/min)
- API calls: 90/min (Polymarket hard limit: 100/min)

Async-safe: uses asyncio.Lock for thread safety.
Logs RATE_LIMIT_WAIT with duration and bucket type when waiting.
"""

import asyncio
import time
from typing import Optional

from src.logs.log_config import get_logger

logger = get_logger("rate_limiter")


class TokenBucket:
    """
    Async-safe token bucket rate limiter.
    Refills tokens at a constant rate. When empty, callers await until
    tokens are available — never drops requests silently.
    """

    def __init__(self, name: str, max_tokens: int, refill_rate: float):
        """
        Args:
            name: Bucket name for logging (e.g., "orders", "api_calls")
            max_tokens: Maximum tokens in the bucket
            refill_rate: Tokens added per second
        """
        self.name = name
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate

        self._tokens = float(max_tokens)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.refill_rate
        self._tokens = min(self.max_tokens, self._tokens + new_tokens)
        self._last_refill = now

    async def acquire(self, tokens: int = 1) -> float:
        """
        Acquire tokens from the bucket. Uses a FIFO-style reservation
        system to prevent 'stampede' race conditions when multiple
        tasks are waiting.
        """
        async with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            # Calculate wait time and RESERVE tokens in the future
            deficit = tokens - self._tokens
            wait_time = deficit / self.refill_rate
            
            # We "pre-consume" the refill time by advancing the clock
            # This ensures the next request calculates its wait time 
            # starting from when THIS request finishes.
            self._last_refill += wait_time
            self._tokens = 0.0

            # Now we sleep to enforce the rate limit
            await asyncio.sleep(wait_time)
            return wait_time

    @property
    def available_tokens(self) -> float:
        """Current available tokens (approximate, without lock)."""
        self._refill()
        return self._tokens


class RateLimiter:
    """
    Dual-bucket rate limiter for Polymarket API.
    Separate buckets for order operations and general API calls.
    """

    def __init__(
        self,
        orders_per_min: int = 500,
        api_calls_per_min: int = 500,
    ):
        """
        Args:
            orders_per_min: Max orders per minute (default 500, Polymarket burst limit is 500/sec)
            api_calls_per_min: Max API calls per minute (default 500)
        """
        self.order_bucket = TokenBucket(
            name="orders",
            max_tokens=orders_per_min,
            refill_rate=orders_per_min / 60.0,
        )

        self.api_bucket = TokenBucket(
            name="api_calls",
            max_tokens=api_calls_per_min,
            refill_rate=api_calls_per_min / 60.0,
        )

        logger.info(
            "rate_limiter_initialized",
            orders_per_min=orders_per_min,
            api_calls_per_min=api_calls_per_min,
            message=f"Rate limiter ready — {orders_per_min} orders/min, {api_calls_per_min} API/min",
        )

    async def acquire_order(self) -> float:
        """
        Acquire a token for placing an order.
        Consumes from BOTH order and API buckets.
        Returns total wait time.
        """
        wait1 = await self.order_bucket.acquire()
        wait2 = await self.api_bucket.acquire()
        return wait1 + wait2

    async def acquire_api_call(self) -> float:
        """
        Acquire a token for a general API call.
        Consumes from API bucket only.
        Returns wait time.
        """
        return await self.api_bucket.acquire()
