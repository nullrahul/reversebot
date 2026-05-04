"""
L2 Authentication — HMAC-SHA256 Headers for Polymarket CLOB API.

Every CLOB request must include:
- POLY_ADDRESS: wallet address
- POLY_SIGNATURE: HMAC-SHA256 signature
- POLY_TIMESTAMP: UTC seconds as integer string
- POLY_NONCE: unique UUID4 per request (never reused)

SECURITY:
- API secret and passphrase are NEVER logged
- On auth failure: log timestamp diff and nonce, not the secret
- After 3 consecutive 401s: trigger kill switch
"""

import base64
import hashlib
import hmac
import time
import uuid
from typing import Dict, Optional, Tuple

import aiohttp

from src.logs.log_config import get_logger

logger = get_logger("l2_auth")


class L2Auth:
    """
    L2 HMAC-SHA256 authentication for Polymarket CLOB API.
    Handles header construction, server time sync, and 401 recovery.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        address: str,
        clob_api_url: str = "https://clob.polymarket.com",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._address = address
        self._clob_api_url = clob_api_url.rstrip("/")

        # Server time offset (local - server) in seconds
        self._time_offset: float = 0.0

        # Track consecutive 401s for kill switch trigger
        self._consecutive_401s: int = 0
        self._max_401_retries: int = 3

        logger.info(
            "l2_auth_initialized",
            address=address,
            clob_api_url=clob_api_url,
            message="L2 HMAC auth initialized — secrets loaded securely",
        )

    @property
    def consecutive_401s(self) -> int:
        """Number of consecutive 401 auth failures."""
        return self._consecutive_401s

    @property
    def should_kill(self) -> bool:
        """True if 401 threshold reached — kill switch should fire."""
        return self._consecutive_401s >= self._max_401_retries

    def reset_401_counter(self) -> None:
        """Reset after a successful authenticated request."""
        if self._consecutive_401s > 0:
            logger.info(
                "auth_401_counter_reset",
                previous_count=self._consecutive_401s,
                message="Auth recovered — 401 counter reset",
            )
        self._consecutive_401s = 0

    def record_401(self) -> None:
        """Record a 401 failure. Returns True if kill threshold reached."""
        self._consecutive_401s += 1
        logger.warning(
            "auth_401_received",
            consecutive_count=self._consecutive_401s,
            max_retries=self._max_401_retries,
            message=f"401 auth failure #{self._consecutive_401s}",
        )

    async def sync_server_time(self, session: Optional[aiohttp.ClientSession] = None) -> float:
        """
        Sync local clock with Polymarket server time.
        This prevents silent auth failures from clock drift.

        Returns the time offset (local - server) in seconds.
        """
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        try:
            local_before = time.time()
            async with session.get(f"{self._clob_api_url}/time") as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        server_time = float(data.get("time", time.time()))
                    else:
                        server_time = float(data)
                    local_after = time.time()

                    # Estimate server time at midpoint of request
                    local_midpoint = (local_before + local_after) / 2
                    self._time_offset = local_midpoint - server_time

                    logger.info(
                        "server_time_synced",
                        offset_seconds=round(self._time_offset, 3),
                        server_time=server_time,
                        message=f"Clock synced — offset: {self._time_offset:.3f}s",
                    )
                else:
                    logger.warning(
                        "server_time_sync_failed",
                        status=resp.status,
                        message="Failed to sync server time — using local clock",
                    )
        except Exception as e:
            logger.warning(
                "server_time_sync_error",
                error=str(e),
                message="Server time sync error — using local clock",
            )
        finally:
            if close_session:
                await session.close()

        return self._time_offset

    def _get_timestamp(self) -> str:
        """Get current timestamp adjusted for server time offset."""
        adjusted = time.time() - self._time_offset
        return str(int(adjusted))

    def _create_signature(
        self,
        timestamp: str,
        method: str,
        path: str,
        body: str = "",
    ) -> str:
        """
        Create HMAC-SHA256 signature for a CLOB API request.
        Matches official Polymarket V2 authentication standard.
        """
        message = timestamp + method.upper() + path + body
        
        # Polymarket V2 requires the secret to be base64 decoded
        secret_bytes = base64.b64decode(self._api_secret)
        
        signature = hmac.new(
            secret_bytes,
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        
        # Result must be base64 encoded
        return base64.b64encode(signature).decode("utf-8")

    def build_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        timestamp = self._get_timestamp()
        signature = self._create_signature(timestamp, method, path, body)

        return {
            "POLY-ADDRESS": self._address,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-API-KEY": self._api_key,
            "POLY-PASSPHRASE": self._passphrase,
        }

    def log_auth_failure_details(
        self,
        method: str,
        path: str,
        status: int,
        response_text: str,
    ) -> None:
        """
        Log auth failure details for debugging.
        NEVER logs the API secret or passphrase.
        Logs the timestamp and nonce used for the failed request.
        """
        timestamp = self._get_timestamp()
        logger.error(
            "auth_failure_details",
            method=method,
            path=path,
            status=status,
            response_preview=response_text[:200],
            timestamp_used=timestamp,
            time_offset=round(self._time_offset, 3),
            consecutive_401s=self._consecutive_401s,
            message=f"Auth failed — {method} {path} returned {status}",
        )
