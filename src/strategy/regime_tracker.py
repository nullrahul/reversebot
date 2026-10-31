"""
Regime Tracker — Tracks BTC/ETH 5-minute candle Persistence Score live.
Runs in Shadow Mode (Soft-Disabled) and writes telemetry to data/regime_telemetry.db.

V2: Direction-aware scoring.
  - Computes P(UP | prev=UP) and P(DOWN | prev=DOWN) separately.
  - Fixes doji bias (strict > for UP).
  - Uses 30-candle lookback for responsiveness.
  - On-demand refresh_score() for fresh pre-trade evaluation.
"""

import asyncio
import time
import aiohttp
import aiosqlite
from pathlib import Path
from src.logs.log_config import get_logger

logger = get_logger("regime_tracker")

# Number of closed candles used for persistence calculation
_LOOKBACK_CANDLES = 30


class RegimeTracker:
    def __init__(self, config=None, db_path="data/regime_telemetry.db"):
        self.config = config
        self.db_path = db_path
        # Direction-aware scores: {"BTC": {"p_up": .., "p_down": .., "overall": ..}, ...}
        self._scores = {
            "BTC": {"p_up": 0.0, "p_down": 0.0, "overall": 0.0},
            "ETH": {"p_up": 0.0, "p_down": 0.0, "overall": 0.0},
        }
        self._last_closed_time = {"BTC": 0, "ETH": 0}
        self._task = None
        self._running = False

    async def start(self) -> None:
        """Start the database initialization, fetch initial data, and run the background poll task."""
        if self._running:
            return
        self._running = True

        # Initialize SQLite database & table
        try:
            db_dir = Path(self.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path, timeout=60.0) as db:
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 60000")
                await db.execute("PRAGMA synchronous = NORMAL")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS persistence_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp_utc INTEGER,
                        asset TEXT,
                        window_minutes INTEGER,
                        persistence_score REAL,
                        p_up_continues REAL,
                        p_down_continues REAL,
                        up_continuations INTEGER,
                        down_continuations INTEGER,
                        total_after_up INTEGER,
                        total_after_down INTEGER,
                        continuations INTEGER,
                        transitions INTEGER
                    )
                    """
                )
                await db.commit()
            logger.info("regime_tracker_db_initialized", db_path=self.db_path)
        except Exception as e:
            logger.error("regime_tracker_db_init_failed", error=str(e))

        # Fetch initial historical klines to calculate initial scores
        await self._fetch_initial_scores()

        # Start background asyncio loop
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("regime_tracker_started")

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("regime_tracker_stopped")

    def get_current_score(self, asset: str, window: int = 5, direction: str = None) -> float:
        """
        Return the current calculated persistence score for the asset.

        Args:
            asset: "BTC" or "ETH"
            window: timeframe in minutes (5 or 15)
            direction: "UP" or "DOWN" for direction-specific score, None for overall

        Returns:
            Conditional continuation probability [0.0, 1.0]
        """
        try:
            window_int = int(window)
        except (ValueError, TypeError):
            window_int = 5

        if window_int not in (5, 15):
            return 0.0

        clean_asset = str(asset).upper().replace("USDT", "")
        scores = self._scores.get(clean_asset)
        if not scores or not isinstance(scores, dict):
            return 0.0
        if direction == "UP":
            return scores.get("p_up", 0.0)
        elif direction == "DOWN":
            return scores.get("p_down", 0.0)
        return scores.get("overall", 0.0)

    def get_current_scores_dict(self, asset: str) -> dict:
        """
        Return the full direction-aware scores dict for dashboard display.
        Returns: {"p_up": .., "p_down": .., "overall": ..}
        """
        clean_asset = str(asset).upper().replace("USDT", "")
        scores = self._scores.get(clean_asset)
        if not scores or not isinstance(scores, dict):
            return {"p_up": 0.0, "p_down": 0.0, "overall": 0.0}
        return scores.copy()

    async def refresh_score(self, asset: str) -> float:
        """
        On-demand fresh score computation. Fetches latest klines from Binance
        and recomputes immediately. Called by DecisionEngine before opening a position
        to ensure the score is never stale.

        Returns the overall score after refresh.
        """
        clean_asset = str(asset).upper().replace("USDT", "")
        symbol = "BTCUSDT" if clean_asset == "BTC" else "ETHUSDT"
        asset_key = clean_asset
        try:
            klines = await self._fetch_klines_from_binance(symbol, "5m", limit=_LOOKBACK_CANDLES + 2)
            if len(klines) < _LOOKBACK_CANDLES + 2:
                logger.warning("regime_tracker_refresh_insufficient_klines", asset=asset_key, count=len(klines))
                return self._scores.get(asset_key, {}).get("overall", 0.0)

            # Use the last N+1 closed candles (excluding the currently forming candle)
            closed_candles = klines[-(_LOOKBACK_CANDLES + 1):-1]
            last_closed_time_ms = klines[-2][0]
            self._last_closed_time[asset_key] = last_closed_time_ms

            result = self._calculate_persistence(closed_candles)
            self._scores[asset_key] = {
                "p_up": result["p_up_continues"],
                "p_down": result["p_down_continues"],
                "overall": result["overall"],
            }

            logger.info(
                "regime_score_refreshed_on_demand",
                asset=asset_key,
                p_up=result["p_up_continues"],
                p_down=result["p_down_continues"],
                overall=result["overall"],
            )
            return result["overall"]
        except Exception as e:
            logger.error("regime_tracker_refresh_failed", asset=asset_key, error=str(e) or type(e).__name__)
            return self._scores.get(asset_key, {}).get("overall", 0.0)

    async def _fetch_initial_scores(self) -> None:
        """Fetch klines from Binance to calculate initial scores for BTC and ETH on startup."""
        for asset in ("BTC", "ETH"):
            symbol = "BTCUSDT" if asset == "BTC" else "ETHUSDT"
            success = False
            for attempt in range(5):
                try:
                    klines = await self._fetch_klines_from_binance(symbol, "5m", limit=_LOOKBACK_CANDLES + 2)
                    if len(klines) >= _LOOKBACK_CANDLES + 2:
                        closed_candles = klines[-(_LOOKBACK_CANDLES + 1):-1]
                        last_closed_candle = klines[-2]
                        last_closed_time_ms = last_closed_candle[0]

                        self._last_closed_time[asset] = last_closed_time_ms

                        result = self._calculate_persistence(closed_candles)
                        self._scores[asset] = {
                            "p_up": result["p_up_continues"],
                            "p_down": result["p_down_continues"],
                            "overall": result["overall"],
                        }

                        logger.info(
                            "regime_tracker_initial_score_loaded",
                            asset=asset,
                            p_up=result["p_up_continues"],
                            p_down=result["p_down_continues"],
                            overall=result["overall"],
                            up_cont=result["up_continuations"],
                            down_cont=result["down_continuations"],
                            last_closed_time=last_closed_time_ms,
                        )
                        success = True
                        break
                    else:
                        logger.warning(
                            "regime_tracker_initial_fetch_insufficient_klines",
                            asset=asset,
                            count=len(klines),
                            attempt=attempt + 1,
                        )
                except Exception as e:
                    logger.warning(
                        "regime_tracker_initial_fetch_failed",
                        asset=asset,
                        attempt=attempt + 1,
                        error=str(e) or type(e).__name__,
                    )
                await asyncio.sleep(2)

            if not success:
                logger.error(
                    "regime_tracker_initial_fetch_abandoned",
                    asset=asset,
                    message="Starting with default score 0.0 due to initial fetch failures",
                )

    async def _fetch_klines_from_binance(self, symbol: str, interval: str, limit: int) -> list:
        """Perform HTTP fetch of klines from Binance REST API."""
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    raise Exception(f"Binance API returned HTTP {resp.status}")

    def _calculate_persistence(self, candles: list) -> dict:
        """
        Calculate direction-specific persistence scores from closed candles.

        Returns dict:
            p_up_continues:    P(UP | previous candle was UP)
            p_down_continues:  P(DOWN | previous candle was DOWN)
            overall:           (all continuations) / (all transitions)
            up_continuations:  count of UP->UP
            down_continuations: count of DOWN->DOWN
            total_after_up:    count of candles that followed an UP candle
            total_after_down:  count of candles that followed a DOWN candle
        """
        if len(candles) < 2:
            return {
                "p_up_continues": 0.5,
                "p_down_continues": 0.5,
                "overall": 0.5,
                "up_continuations": 0,
                "down_continuations": 0,
                "total_after_up": 0,
                "total_after_down": 0,
            }

        directions = []
        for c in candles:
            open_p = float(c[1])
            close_p = float(c[4])
            # Strict > for UP: doji (close == open) counts as DOWN to avoid UP bias
            direction = 1 if close_p > open_p else -1
            directions.append(direction)

        up_after_up = 0
        down_after_down = 0
        total_after_up = 0
        total_after_down = 0

        for i in range(1, len(directions)):
            if directions[i - 1] == 1:  # Previous candle was UP
                total_after_up += 1
                if directions[i] == 1:
                    up_after_up += 1
            else:  # Previous candle was DOWN
                total_after_down += 1
                if directions[i] == -1:
                    down_after_down += 1

        p_up = up_after_up / total_after_up if total_after_up > 0 else 0.5
        p_down = down_after_down / total_after_down if total_after_down > 0 else 0.5

        overall_continuations = up_after_up + down_after_down
        overall_transitions = len(directions) - 1
        overall = overall_continuations / overall_transitions if overall_transitions > 0 else 0.5

        return {
            "p_up_continues": round(p_up, 4),
            "p_down_continues": round(p_down, 4),
            "overall": round(overall, 4),
            "up_continuations": up_after_up,
            "down_continuations": down_after_down,
            "total_after_up": total_after_up,
            "total_after_down": total_after_down,
        }

    async def _save_to_db(
        self, timestamp_utc: int, asset: str, window_minutes: int, result: dict
    ) -> None:
        """Save a recalculated score event to persistence_history table."""
        try:
            async with aiosqlite.connect(self.db_path, timeout=60.0) as db:
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 60000")
                await db.execute("PRAGMA synchronous = NORMAL")
                await db.execute(
                    """
                    INSERT INTO persistence_history (
                        timestamp_utc, asset, window_minutes, persistence_score,
                        p_up_continues, p_down_continues,
                        up_continuations, down_continuations,
                        total_after_up, total_after_down,
                        continuations, transitions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp_utc,
                        asset,
                        window_minutes,
                        result["overall"],
                        result["p_up_continues"],
                        result["p_down_continues"],
                        result["up_continuations"],
                        result["down_continuations"],
                        result["total_after_up"],
                        result["total_after_down"],
                        result["up_continuations"] + result["down_continuations"],
                        result["total_after_up"] + result["total_after_down"],
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error("regime_tracker_save_failed", asset=asset, error=str(e))

    async def _poll_loop(self) -> None:
        """Background loop executing checks every 60 seconds."""
        while self._running:
            try:
                await self._update_scores()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("regime_tracker_poll_error", error=str(e) or type(e).__name__)

            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    async def _update_scores(self) -> None:
        """Fetch latest klines, detect if a new 5m candle has closed, recalculate and store."""
        for asset in ("BTC", "ETH"):
            symbol = "BTCUSDT" if asset == "BTC" else "ETHUSDT"
            try:
                klines = await self._fetch_klines_from_binance(symbol, "5m", limit=_LOOKBACK_CANDLES + 2)
                if len(klines) < _LOOKBACK_CANDLES + 2:
                    logger.warning("regime_tracker_poll_insufficient_klines", asset=asset, count=len(klines))
                    continue

                last_closed_candle = klines[-2]
                last_closed_time_ms = last_closed_candle[0]

                # Check if it's a new closed candle
                if last_closed_time_ms > self._last_closed_time.get(asset, 0):
                    self._last_closed_time[asset] = last_closed_time_ms

                    closed_candles = klines[-(_LOOKBACK_CANDLES + 1):-1]
                    result = self._calculate_persistence(closed_candles)
                    self._scores[asset] = {
                        "p_up": result["p_up_continues"],
                        "p_down": result["p_down_continues"],
                        "overall": result["overall"],
                    }

                    logger.info(
                        "regime_score_recalculated",
                        asset=asset,
                        p_up=result["p_up_continues"],
                        p_down=result["p_down_continues"],
                        overall=result["overall"],
                        up_cont=result["up_continuations"],
                        down_cont=result["down_continuations"],
                        last_closed_time=last_closed_time_ms,
                    )

                    await self._save_to_db(
                        timestamp_utc=int(last_closed_time_ms // 1000),
                        asset=asset,
                        window_minutes=5,
                        result=result,
                    )
            except Exception as e:
                logger.error("regime_tracker_update_failed", asset=asset, error=str(e) or type(e).__name__)
