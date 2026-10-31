"""
Binance WebSocket Client — Real-time CVD tracking for Polymarket Reverse Bot.
Aggregates aggTrades for BTCUSDT and ETHUSDT from Binance to calculate 60s and 5m CVD.
"""

import asyncio
import json
import time
from collections import deque
from typing import Dict, Tuple, Optional
import aiohttp

from src.logs.log_config import get_logger

logger = get_logger("binance_ws")


class BinanceWSClient:
    """
    WebSocket client that streams aggTrades from Binance for BTCUSDT and ETHUSDT,
    computing real-time Cumulative Volume Delta (CVD) over 60s and 5m windows.
    """

    def __init__(self):
        self._url = "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade"
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._should_run = False

        # Trade deques: deque of (timestamp, delta_usd)
        self._trades: Dict[str, deque] = {
            "BTC": deque(),
            "ETH": deque(),
        }

        # Track the latest spot price from aggTrades
        self._last_price: Dict[str, float] = {
            "BTC": 0.0,
            "ETH": 0.0,
        }

        # Track 1-second price ticks and velocity for Z-score calculation
        self._price_history_1s: Dict[str, deque] = {
            "BTC": deque(maxlen=300),  # stores (timestamp_sec, price)
            "ETH": deque(maxlen=300),
        }
        self._velocity_history_1s: Dict[str, deque] = {
            "BTC": deque(maxlen=300),  # stores velocity values (price_t - price_t-1)
            "ETH": deque(maxlen=300),
        }

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def get_last_price(self, symbol: str) -> float:
        return self._last_price.get(symbol.upper(), 0.0)

    async def start(self) -> None:
        """Start the WebSocket subscription background loop."""
        self._should_run = True
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("binance_ws_started", message="Binance WS client background task launched")

    async def stop(self) -> None:
        """Stop the background loop and close connections."""
        self._should_run = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("binance_ws_stopped", message="Binance WS client stopped")

    async def _run_loop(self) -> None:
        backoff = 1.0
        while self._should_run:
            try:
                self._session = aiohttp.ClientSession()
                async with self._session.ws_connect(self._url) as ws:
                    self._ws = ws
                    backoff = 1.0
                    logger.info("binance_ws_connected", url=self._url)

                    async for msg in ws:
                        if not self._should_run:
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("binance_ws_closed_by_remote")
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("binance_ws_error_event")
                            break

            except Exception as e:
                logger.error("binance_ws_connection_error", error=str(e))

            finally:
                if self._session and not self._session.closed:
                    await self._session.close()
                self._ws = None

            if self._should_run:
                logger.info("binance_ws_reconnecting", delay=backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _handle_message(self, raw_data: str) -> None:
        try:
            payload = json.loads(raw_data)
            stream = payload.get("stream", "")
            data = payload.get("data", {})

            # Parse symbol
            if "btcusdt" in stream:
                symbol = "BTC"
            elif "ethusdt" in stream:
                symbol = "ETH"
            else:
                return

            price = float(data.get("p", 0.0))
            quantity = float(data.get("q", 0.0))
            is_buyer_maker = data.get("m", False)  # True = Sell Taker, False = Buy Taker

            # Calculate trade USD delta
            usd_value = price * quantity
            delta_usd = -usd_value if is_buyer_maker else usd_value

            # Save latest price
            self._last_price[symbol] = price

            # Track 1-second price ticks and price velocity
            now = time.time()
            now_sec = int(now)
            price_hist = self._price_history_1s[symbol]
            if not price_hist or price_hist[-1][0] != now_sec:
                if price_hist:
                    prev_sec, prev_price = price_hist[-1]
                    dt = now_sec - prev_sec
                    if 0 < dt <= 5:  # prevent giant jumps on reconnection/inactivity
                        vel = (price - prev_price) / dt
                        self._velocity_history_1s[symbol].append(vel)
                price_hist.append((now_sec, price))
            else:
                # Update price for the current second
                price_hist[-1] = (now_sec, price)

            # Add to deque
            self._trades[symbol].append((now, delta_usd))

            # Prune old trades (older than 5 minutes / 300s)
            self._prune_old_trades(symbol, now)

        except Exception as e:
            logger.error("binance_ws_parse_failed", error=str(e))

    def _prune_old_trades(self, symbol: str, now: float) -> None:
        q = self._trades[symbol]
        cutoff = now - 300.0
        while q and q[0][0] < cutoff:
            q.popleft()

    def get_cvd(self, symbol: str, window_seconds: int = 60) -> float:
        """
        Calculates the Cumulative Volume Delta (CVD) for a symbol within the last N seconds.
        """
        symbol = symbol.upper()
        if symbol not in self._trades:
            return 0.0

        now = time.time()
        cutoff = now - window_seconds
        q = self._trades[symbol]

        cvd = 0.0
        for timestamp, delta in reversed(q):
            if timestamp >= cutoff:
                cvd += delta
            else:
                break
        return cvd

    def get_cvd_metrics(self, symbol: str) -> Tuple[float, float]:
        """
        Returns (cvd_60s, cvd_5m) for the given symbol.
        """
        symbol = symbol.upper()
        return self.get_cvd(symbol, 60), self.get_cvd(symbol, 300)

    def get_spot_velocity_z_score(self, symbol: str) -> float:
        """
        Calculates the rolling Z-score of 1-second price velocity over the last 5 minutes.
        """
        import math
        symbol = symbol.upper()
        if symbol not in self._velocity_history_1s:
            return 0.0
            
        velocities = list(self._velocity_history_1s[symbol])
        if len(velocities) < 30:  # Need at least 30 samples to calculate a stable standard deviation
            return 0.0
            
        mean = sum(velocities) / len(velocities)
        variance = sum((x - mean) ** 2 for x in velocities) / len(velocities)
        std_dev = math.sqrt(variance)
        if std_dev < 1e-4:
            return 0.0
            
        current_vel = velocities[-1]
        z = (current_vel - mean) / std_dev
        return z

    def get_last_velocity(self, symbol: str) -> float:
        """Returns the latest 1-second price velocity."""
        symbol = symbol.upper()
        velocities = self._velocity_history_1s.get(symbol)
        if velocities:
            return velocities[-1]
        return 0.0
