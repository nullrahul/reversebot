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
        Acquire tokens from the bucket. If not enough tokens,
        await until they're available.

        Args:
            tokens: Number of tokens to acquire (default 1)

        Returns:
            Wait time in seconds (0.0 if no wait needed)
        """
        total_wait = 0.0

        async with self._lock:
            self._refill()

            while self._tokens < tokens:
                # Calculate wait time for enough tokens
                deficit = tokens - self._tokens
                wait_time = deficit / self.refill_rate

                logger.warning(
                    "rate_limit_wait",
                    bucket=self.name,
                    wait_seconds=round(wait_time, 3),
                    tokens_available=round(self._tokens, 2),
                    tokens_needed=tokens,
                    message=f"Rate limit — waiting {wait_time:.2f}s for {self.name} bucket",
                )

                total_wait += wait_time

                # Release lock while waiting so other tasks can proceed
                # We'll re-check after sleep
                self._lock.release()
                await asyncio.sleep(wait_time)
                await self._lock.acquire()
                self._refill()

            self._tokens -= tokens

        return total_wait

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
        orders_per_min: int = 55,
        api_calls_per_min: int = 240,
    ):
        """
        Args:
            orders_per_min: Max orders per minute (default 55, hard limit 60)
            api_calls_per_min: Max API calls per minute (default 90, hard limit 100)
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
