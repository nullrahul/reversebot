"""
Exception Hierarchy — RuFlo V3 Reverse Bot.

Provides typed exceptions for distinguishing transient vs permanent failures.
This allows the bot to:
  - Auto-retry on transient network errors (timeouts, resets, broken pipes)
  - Halt immediately on permanent errors (auth failures, invalid params)
  - Log order rejections without triggering kill switch

Usage in clob_client.py / websocket.py:
    try:
        resp = await session.post(...)
    except (aiohttp.ClientConnectorError,
            aiohttp.ServerDisconnectedError,
            asyncio.TimeoutError,
            ConnectionResetError,
            BrokenPipeError) as e:
        raise TransientNetworkError(str(e)) from e
"""


class BotException(Exception):
    """Base exception for all bot errors."""
    pass


class TransientNetworkError(BotException):
    """
    Retry-safe network error.

    Raised on: timeouts, connection resets, broken pipes, DNS failures.
    Handler should: wait + retry with exponential backoff.
    Should NOT trigger kill switch (up to threshold).
    """
    pass


class PermanentApiError(BotException):
    """
    Non-retryable API error.

    Raised on: 401 Unauthorized, 403 Forbidden, invalid parameters.
    Handler should: log, increment kill switch counter, do NOT retry.
    """

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class OrderRejectedError(BotException):
    """
    Exchange rejected the order.

    Raised on: FOK not filled, insufficient balance, market closed.
    Handler should: log rejection, update position status, move on.
    Should NOT trigger kill switch.
    """

    def __init__(self, message: str, order_id: str = ""):
        super().__init__(message)
        self.order_id = order_id


class WebSocketDroppedError(TransientNetworkError):
    """
    WebSocket connection was lost unexpectedly.

    Raised on: BrokenPipeError or ConnectionResetError during WS operations.
    Handler should: clear price_cache for affected tokens, reconnect.
    """
    pass


class GhostPositionError(BotException):
    """
    Position stuck in CLOSE_FAILED state.

    Raised when a position has failed to close and exceeded max retry attempts.
    Handler should: log to Telegram, flag for manual intervention.
    """

    def __init__(self, position_id: str, attempts: int):
        super().__init__(f"Position {position_id} stuck after {attempts} close attempts")
        self.position_id = position_id
        self.attempts = attempts
