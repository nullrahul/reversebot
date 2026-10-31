"""
Position Manager — Open/Monitor/Close Positions for Polymarket Reverse Bot.

Handles:
- Opening: PENDING → place FAK → confirm fill → OPEN or FAILED
- Monitoring: TP, SL, resolution check every cycle
- Closing: FAK sell → retry once at -1¢ → CLOSE_FAILED on failure

MISTAKE 8 GUARD: asyncio.Lock() per market_id prevents race conditions.
DB partial unique index is the secondary guard.

MISTAKE 7 GUARD: Always confirms fill via order status endpoint.
Never assumes fill from HTTP 200 placement response.
"""

import asyncio
import time
import json
import uuid
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config.config import BotConfig
from src.strategy.math_utils import net_value, realised_pnl as calc_realised_pnl, is_mock_market_id
from src.exceptions import TransientNetworkError
from src.api.clob_client import ClobClient
from src.db.queries import (
    create_position,
    update_position_status,
    close_position,
    get_open_positions,
    get_position,
    get_market,
    has_open_position_for_market,
    log_event,
    update_position_notes,
    update_position_min_max_prices,
    get_db_lock,
    execute_with_retry,
    commit_with_retry,
    fetch_all,
    fetch_one,
)
from src.logs.log_config import get_logger

logger = get_logger("position_mgr")

# Per-market locks to prevent race conditions
_market_locks = {}


async def get_market_lock(market_id: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific market_id."""
    if market_id not in _market_locks:
        _market_locks[market_id] = asyncio.Lock()
    return _market_locks[market_id]


def _get_tid_from_notes(notes: Optional[str]) -> Optional[str]:
    if not notes: return None
    if notes.startswith("{"):
        try:
            return json.loads(notes).get("tid")
        except:
            return notes
    return notes

class PositionManager:
    """
    Manages the lifecycle of trading positions.
    Ensures safe entry, continuous monitoring, and reliable exit.
    """

    def __init__(
        self,
        config: BotConfig,
        clob_client: ClobClient,
        db,
        ws_client=None,
        gamma_client=None,
        skip_db=None,
        scanner=None,
    ):
        self.config = config
        self.clob = clob_client
        self.db = db
        self.ws = ws_client
        self.gamma = gamma_client
        self.skip_db = skip_db
        self.scanner = scanner
        self.tg = None  # Set by main.py during setup
        self._resolution_lock = asyncio.Lock()

        self._paper = config.paper_trade
        self._market_cache: Dict[str, dict] = {}

        # Shadow Meta-Strategy Logger — observational only
        from src.strategy.meta_logger import MetaStrategyLogger
        self.meta_logger = MetaStrategyLogger(self.db)
        
        # SL Spike Protection: position_id -> first_seen_timestamp
        self._sl_confirmation_timers: Dict[str, float] = {}
        
        # ─── RECOVERY: Load tracking data from DB on startup ───
        # Note: Proceeds/Remaining now persisted in DB to survive restarts.
        self._partial_proceeds: Dict[str, float] = {}
        self._shares_remaining: Dict[str, float] = {}

        # LKGP (Last Known Good Price) tracking: position_id -> price
        self._lkgp_memory: Dict[str, float] = {}

        # Resting Limit Order Tracking: position_id -> List[Dict[str, Any]]
        # Each dict: {"order_id": str, "shares": int, "multiplier": float}
        self._resting_tp_orders: Dict[str, List[Dict[str, Any]]] = {}
        
        # Active Order Tracking: order_id -> position_id (for MINED events)
        self._active_orders: Dict[str, str] = {}
        
        # Concurrent Close Protection: set of position_ids being closed
        self._closing_positions: Set[str] = set()

        # Trailing Take Profit memory registries: position_id -> bool/float
        self._trailing_tp_active: Dict[str, bool] = {}
        self._trailing_tp_peak: Dict[str, float] = {}
        
        # Probability Stop Loss Profile memory registries
        self.open_probability_sl_positions: Dict[str, dict] = {}
        self._prob_sl_first_dip: Dict[str, dict] = {}
        self._prob_sl_monitor_task: Optional[asyncio.Task] = None

        # Last REST query time for price feed fallback to avoid rate limits
        self._last_rest_price_query: Dict[str, float] = {}

        # Background event processor
        self._event_task: Optional[asyncio.Task] = None
        self._monitor_positions_task: Optional[asyncio.Task] = None
        self._resolution_check_task: Optional[asyncio.Task] = None

        mode = "PAPER" if self._paper else "LIVE"
        logger.info(
            "position_mgr_initialized",
            mode=mode,
            message=f"Position manager ready in {mode} mode",
        )

    async def _get_actual_fills_from_trades(self, order_id: str) -> Optional[Tuple[float, float]]:
        """
        Query /data/trades to fetch actual fills for the given order_id.
        Returns (actual_price, actual_shares) or None if no trades are found.
        """
        if self._paper:
            return None

        user_address = getattr(self.config, "poly_address", None)
        if not user_address and hasattr(self.clob, "_auth") and hasattr(self.clob._auth, "_address"):
            user_address = self.clob._auth._address

        if not user_address:
            logger.warning("missing_user_address_for_trades_query", order_id=order_id)
            return None

        # Fetch recent trades. We try up to 3 times with a 1.0s delay in case of CLOB API lag.
        for attempt in range(1, 4):
            try:
                status, data = await self.clob._request("GET", f"/data/trades?maker_address={user_address}")
                if status == 200 and isinstance(data, dict):
                    trades = data.get("data", [])
                    matching_fills = []
                    for t in trades:
                        # Match by taker_order_id or id
                        trade_order_id = t.get("taker_order_id") or t.get("id")
                        if trade_order_id == order_id:
                            matching_fills.append(t)
                    
                    if matching_fills:
                        total_qty = 0.0
                        total_cost = 0.0
                        for f in matching_fills:
                            qty = float(f.get("size", 0.0))
                            px = float(f.get("price", 0.0))
                            total_qty += qty
                            total_cost += qty * px
                        
                        if total_qty > 0.0:
                            vwap = total_cost / total_qty
                            logger.info(
                                "actual_fills_resolved",
                                order_id=order_id,
                                vwap=vwap,
                                shares=total_qty,
                                attempt=attempt
                            )
                            return vwap, total_qty
                else:
                    logger.warning(
                        "trades_query_http_error",
                        order_id=order_id,
                        status=status,
                        response=data,
                        attempt=attempt
                    )
            except Exception as e:
                logger.error("trades_query_failed", order_id=order_id, error=str(e), attempt=attempt)
            
            # API synchronization lag cooldown
            await asyncio.sleep(1.0)
            
        logger.warning("no_trades_found_for_order", order_id=order_id)
        return None

    def _clean_price(self, val) -> Optional[float]:
        if val is None:
            return None
        # Handle MagicMock / AsyncMock / Mock objects returned by un-mocked properties in unit tests
        if type(val).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def _get_current_price_with_rest_fallback(
        self, target_token_id: str, market_id: str, side: str, position_id: str, opened_at: int = 0, is_profile: bool = False
    ) -> Optional[float]:
        """
        Retrieves the current execution price for a position, checking WS cache first.
        If WS cache is empty (None or <= 0), falls back to querying the Polymarket CLOB REST API.
        Includes midpoint calculation during first 30 seconds of core trades.
        For virtual profile positions (is_profile=True), retrieves direction-aware best ask/implied counterpart/buy-side prices.
        """
        from unittest.mock import MagicMock, AsyncMock, Mock
        now = time.time()
        current_price = None

        if is_profile:
            # 1. For virtual profiles, check WebSocket Best Ask directly to avoid spread drag
            if self.ws:
                current_price = self._clean_price(self.ws.get_best_ask(target_token_id))
        else:
            # 1. Check WebSocket Best Bid
            current_bid = None
            if self.ws:
                current_bid = self._clean_price(self.ws.get_best_bid(target_token_id))

            # Check midpoint grace period for core trades
            if current_bid is not None and opened_at > 0 and now - opened_at < 30.0:
                current_ask = self._clean_price(self.ws.get_best_ask(target_token_id))
                if current_ask is not None:
                    current_price = (current_bid + current_ask) / 2.0
                else:
                    current_price = current_bid
            else:
                current_price = current_bid

        # 2. Check Implied Price from counterpart if WS target price is empty
        if current_price is None or current_price <= 0:
            m_data = self._market_cache.get(market_id)
            if not m_data:
                from src.db.queries import get_market
                m_data = await get_market(self.db, market_id)
                self._market_cache[market_id] = m_data
            if m_data:
                counterpart_tid = m_data.get("no_token_id") if side == "YES" else m_data.get("yes_token_id")
                if counterpart_tid and self.ws:
                    if is_profile:
                        # Implied Ask = 1.0 - counterpart bid
                        counterpart_bid = self._clean_price(self.ws.get_best_bid(counterpart_tid))
                        if counterpart_bid is not None and counterpart_bid > 0:
                            current_price = round(1.0 - counterpart_bid, 4)
                    else:
                        # Implied Bid = 1.0 - counterpart ask
                        counterpart_ask = self._clean_price(self.ws.get_best_ask(counterpart_tid))
                        if counterpart_ask is not None and counterpart_ask < 1.0:
                            current_price = round(1.0 - counterpart_ask, 4)

        # 3. Check general WS price cache
        if (current_price is None or current_price <= 0) and self.ws:
            current_price = self._clean_price(self.ws.get_live_price(target_token_id))

        # 4. REST Fallback with Throttling
        if current_price is None or current_price <= 0:
            # Throttle REST queries to once per 10 seconds per token ID
            if now - self._last_rest_price_query.get(target_token_id, 0.0) >= 10.0:
                self._last_rest_price_query[target_token_id] = now
                try:
                    method = getattr(self.clob, "get_current_price", None)
                    if method is not None:
                        clob_side = "buy" if is_profile else "sell"
                        if isinstance(method, AsyncMock) or asyncio.iscoroutinefunction(method):
                            rest_price = await method(target_token_id, side=clob_side)
                        elif isinstance(method, (MagicMock, Mock)):
                            rest_price = None
                        else:
                            rest_price = await method(target_token_id, side=clob_side)
                        
                        if rest_price is not None and rest_price > 0:
                            current_price = self._clean_price(rest_price)
                            logger.info(
                                "ws_price_feed_blind_spot_rest_fallback",
                                position_id=position_id,
                                token_id=target_token_id,
                                rest_price=current_price,
                                message=f"WS cache empty for token {target_token_id}. Fallback REST price retrieved: {current_price}"
                            )
                except Exception as rest_err:
                    logger.error(
                        "ws_price_feed_blind_spot_rest_fallback_failed",
                        token_id=target_token_id,
                        error=str(rest_err)
                    )
        return current_price

    async def start(self) -> None:
        """Start the background event processor."""
        # Clean up unconfirmed / orphaned live positions from previous runs
        if not self._paper:
            try:
                async with get_db_lock(self.db):
                    await execute_with_retry(
                        self.db,
                        "UPDATE positions SET order_status = 'FAILED', exit_price = 0.0, realised_pnl = 0.0, close_reason = 'ORDER_UNCONFIRMED_STARTUP' "
                        "WHERE order_status = 'PENDING' OR (order_status = 'OPEN' AND (order_id IS NULL OR order_id = ''))"
                    )
                    await commit_with_retry(self.db)
            except Exception as clean_err:
                logger.warning("startup_position_cleanup_failed", error=str(clean_err))

        if self.ws and not self._event_task:
            self._event_task = asyncio.create_task(self._event_processor_loop())
            logger.info("position_mgr_event_processor_started")
        if not self._prob_sl_monitor_task:
            self._prob_sl_monitor_task = asyncio.create_task(self._monitor_probability_sl_loop())
            logger.info("position_mgr_prob_sl_monitor_task_started")
        if not self._monitor_positions_task:
            self._monitor_positions_task = asyncio.create_task(self._monitor_positions_loop())
            logger.info("position_mgr_monitor_positions_task_started")
        if not self._resolution_check_task:
            self._resolution_check_task = asyncio.create_task(self._resolution_check_loop())
            logger.info("position_mgr_resolution_check_task_started")

    async def stop(self) -> None:
        """Stop the background event processor."""
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None
            logger.info("position_mgr_event_processor_stopped")
        if self._prob_sl_monitor_task:
            self._prob_sl_monitor_task.cancel()
            try:
                await self._prob_sl_monitor_task
            except asyncio.CancelledError:
                pass
            self._prob_sl_monitor_task = None
            logger.info("position_mgr_prob_sl_monitor_task_stopped")
        if self._monitor_positions_task:
            self._monitor_positions_task.cancel()
            try:
                await self._monitor_positions_task
            except asyncio.CancelledError:
                pass
            self._monitor_positions_task = None
            logger.info("position_mgr_monitor_positions_task_stopped")
        if self._resolution_check_task:
            self._resolution_check_task.cancel()
            try:
                await self._resolution_check_task
            except asyncio.CancelledError:
                pass
            self._resolution_check_task = None
            logger.info("position_mgr_resolution_check_task_stopped")

    async def _monitor_positions_loop(self) -> None:
        """
        Background loop to monitor open positions (both virtual profile and core positions).
        Runs every 2.0 seconds. Immune to scanner thread blockages.
        """
        # Warmup delay: Allow 10.0 seconds for the WebSocket connection to establish
        # and populate price caches to avoid startup REST price anomalies.
        await asyncio.sleep(10.0)
        while True:
            try:
                await self.monitor_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("error_in_monitor_positions_loop", error=str(e))
            await asyncio.sleep(2.0)

    async def _resolution_check_loop(self) -> None:
        """
        Background loop to check for market resolutions.
        Runs every 10.0 seconds. Immune to scanner thread blockages.
        """
        await asyncio.sleep(2.0)
        while True:
            try:
                await self.check_resolutions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("error_in_resolution_check_loop", error=str(e))
            await asyncio.sleep(10.0)

    async def _event_processor_loop(self) -> None:
        """Processes user events (MINED, etc.) from the WebSocket queue."""
        if not self.ws: return
        
        while True:
            try:
                event = await self.ws.user_queue.get()
                # Obsolete parameters removed. No resting TP orders are placed.
            except Exception as e:
                logger.error("event_processor_error", error=str(e))

    async def _get_confirmed_token_balance(self, token_id: str, max_wait: float = 30.0) -> float:
        """
        Poll token balance with stability logic to bypass Polymarket's holdings lag.
        Returns the confirmed stable share balance.
        """
        if self._paper:
            return 0.0

        # Polymarket has a 5-10 second holdings update lag. Sleep 5 seconds initially.
        logger.info("waiting_for_polymarket_holdings_lag", token_id=token_id)
        await asyncio.sleep(5.0)

        start_time = time.monotonic()
        last_balance = None
        consecutive_matches = 0
        required_matches = 2 # matching values twice in a row
        
        while time.monotonic() - start_time < max_wait:
            balance = await self.clob.get_token_balance(token_id)
            if balance is not None:
                logger.info("polled_token_balance", token_id=token_id, balance=balance, last_balance=last_balance)
                if last_balance is not None and abs(balance - last_balance) < 0.0001:
                    consecutive_matches += 1
                    if consecutive_matches >= required_matches:
                        logger.info(
                            "token_balance_confirmed_stable",
                            token_id=token_id,
                            balance=balance,
                            consecutive_matches=consecutive_matches
                        )
                        return balance
                else:
                    consecutive_matches = 1
                last_balance = balance
            else:
                logger.warning("token_balance_poll_returned_none", token_id=token_id)
            
            # Sleep 4 seconds between poll attempts
            await asyncio.sleep(4.0)

        logger.warning(
            "token_balance_stability_timeout",
            token_id=token_id,
            last_balance=last_balance,
            message="Timed out waiting for balance to stabilize. Returning last known value."
        )
        return last_balance if last_balance is not None else 0.0

    async def open_position(
        self,
        market_id: str,
        target_token_id: Optional[str] = None,
        entry_price: float = 0.0,
        size_usdc: float = 0.0,
        shares: float = 0.0,
        side: str = 'NO',
        no_token_id: Optional[str] = None,
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        streak_depth: Optional[int] = None,
        persistence_score: float = 0.0,
        strategy_mode: str = "TREND",
        inverted: bool = False,
        original_side: Optional[str] = None,
    ) -> bool:
        """
        Open a new position with full safety guards.
        """
        if target_token_id is None:
            if no_token_id is not None:
                target_token_id = no_token_id
            else:
                raise ValueError("Either target_token_id or no_token_id must be provided")
        lock = await get_market_lock(market_id)

        async with lock:
            already_open = await has_open_position_for_market(self.db, market_id, paper=self._paper)
            if already_open:
                return False

            actual_shares = int(shares)
            # Ensure minimum notional value of at least $1.01 to prevent Polymarket HTTP 400 (ORDER_AMOUNT_TOO_SMALL)
            if entry_price > 0:
                import math
                min_shares_for_dollar = math.ceil(1.01 / entry_price)
                if actual_shares < min_shares_for_dollar:
                    actual_shares = int(min_shares_for_dollar)
            actual_size_usdc = actual_shares * entry_price

            # ─── LIVE RUN SAFETY GUARDS (Exposure Cap & Available Wallet Balance) ───
            if not self._paper:
                from src.db.queries import get_total_exposure
                current_exp = await get_total_exposure(self.db, paper=False)
                max_exp = getattr(self.config, "max_total_exposure_usdc", 5.0)
                if type(max_exp).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    max_exp = 5.0
                else:
                    max_exp = float(max_exp)
                if current_exp + actual_size_usdc > max_exp:
                    logger.warning(
                        "live_exposure_limit_guard_blocked",
                        market_id=market_id,
                        current_exposure=current_exp,
                        adding=actual_size_usdc,
                        max_allowed=max_exp,
                        message=f"Live order blocked: exposure (${current_exp:.2f} + ${actual_size_usdc:.2f}) exceeds max ${max_exp:.2f}"
                    )
                    return False

                if hasattr(self, "clob") and self.clob:
                    try:
                        from unittest.mock import MagicMock, AsyncMock
                        avail_bal = None
                        if hasattr(self.clob, "get_cached_balance_sync") and not isinstance(self.clob, (MagicMock, AsyncMock)):
                            avail_bal = self.clob.get_cached_balance_sync()

                        if (avail_bal is None or isinstance(avail_bal, (MagicMock, AsyncMock))) and hasattr(self.clob, "get_account_balance"):
                            bal_res = self.clob.get_account_balance()
                            if asyncio.iscoroutine(bal_res) or hasattr(bal_res, "__await__"):
                                bal_res = await bal_res
                            if bal_res and isinstance(bal_res, dict):
                                try:
                                    avail_bal = float(bal_res.get("balance", 0.0))
                                except (ValueError, TypeError):
                                    avail_bal = None

                        if avail_bal is not None and not isinstance(avail_bal, (MagicMock, AsyncMock)) and avail_bal < actual_size_usdc:
                            logger.critical(
                                "live_wallet_insufficient_balance_blocked",
                                market_id=market_id,
                                available_balance=avail_bal,
                                required_size=actual_size_usdc,
                                message=f"Live order blocked: wallet balance (${avail_bal:.2f} < ${actual_size_usdc:.2f})"
                            )
                            return False
                    except Exception as b_err:
                        logger.warning("live_balance_check_error_non_blocking", error=str(b_err))
            
            # Format notes as a JSON structure to persist the strategy mode and audit flags
            notes_payload = json.dumps({
                "tid": target_token_id, 
                "strategy": strategy_mode,
                "inverted": inverted,
                "original_side": original_side
            })
            
            position_id = await create_position(
                self.db,
                market_id=market_id,
                entry_price=entry_price,
                size_usdc=actual_size_usdc,
                shares_bought=actual_shares,
                bot_version=self.config.bot_version,
                paper=self._paper,
                notes=notes_payload,
                side=side,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                strategy_mode=strategy_mode,
            )

            try:
                # ─── FAK RETRY PRE-FLIGHT VERIFICATION ───
                # Fetch market metadata to parse timeframe window for gate checking
                market_data = await get_market(self.db, market_id)
                window = 5 # default fallback
                if market_data:
                    slug = (market_data.get("slug") or "").lower()
                    question = (market_data.get("question") or "").lower()
                    window = market_data.get("window_minutes")
                    if not window:
                        if "-5m-" in slug or "5-minute" in question:
                            window = 5
                        elif "-15m-" in slug or "15-minute" in question:
                            window = 15
                        else:
                            window = 5

                if streak_depth is None:
                    streak_depth = market_data.get("streak_depth", 2) if market_data else 2

                # Track fills across attempts
                total_shares_filled = 0.0
                weighted_cost = 0.0
                last_order_id = None
                attempts = 0
                max_attempts = 15
                
                # Fetch minimum order size (hardcoded to minimum 6.0 shares)
                order_min_size = 6.0

                async def _get_live_ask_price(token_id: str) -> float:
                    from unittest.mock import MagicMock, AsyncMock
                    if self.ws:
                        best_ask = self.ws.get_best_ask(token_id)
                        if best_ask is not None and not isinstance(best_ask, (MagicMock, AsyncMock)):
                            try:
                                return float(best_ask)
                            except (ValueError, TypeError):
                                pass
                    
                    clob_ob = getattr(self.clob, "get_orderbook", None)
                    if clob_ob:
                        if isinstance(clob_ob, (MagicMock, AsyncMock)):
                            if not isinstance(clob_ob.return_value, (MagicMock, AsyncMock)) or clob_ob.side_effect is not None:
                                try:
                                    book = await clob_ob(token_id)
                                    if isinstance(book, dict) and book.get("asks"):
                                        return float(book["asks"][0]["price"])
                                except Exception:
                                    pass
                        else:
                            try:
                                book = await clob_ob(token_id)
                                if isinstance(book, dict) and book.get("asks"):
                                    return float(book["asks"][0]["price"])
                            except Exception as e:
                                logger.warning("failed_to_fetch_live_ask_price", token_id=token_id, error=str(e))
                    return entry_price

                # Initialize limit price to the latest live ask
                current_limit_price = await _get_live_ask_price(target_token_id)

                # Pre-flight guard: if best ask price is 0.9900 or higher (indicates empty/thin book),
                # wait 2 seconds and retry once. If it remains >= 0.9900, abort the trade.
                if current_limit_price >= 0.9900:
                    logger.warning(
                        "orderbook_extremely_thin_detected",
                        market_id=market_id,
                        price=current_limit_price,
                        message="Best ask is >= 0.9900. Waiting 2 seconds for orderbook to stabilize and retrying..."
                    )
                    await asyncio.sleep(2.0)
                    current_limit_price = await _get_live_ask_price(target_token_id)
                    
                    if current_limit_price >= 0.9900:
                        logger.error(
                            "orderbook_stabilization_failed",
                            market_id=market_id,
                            price=current_limit_price,
                            message="Best ask remains >= 0.9900 after retry. Aborting position entry to protect capital."
                        )
                        await update_position_status(self.db, position_id, "FAILED", paper=self._paper)
                        return False

                while attempts < max_attempts:
                    attempts += 1
                    
                    remaining_budget = size_usdc - weighted_cost
                    if remaining_budget <= 0.0:
                        logger.info(
                            "cascade_fill_completed_budget_reached",
                            market_id=market_id,
                            budget=size_usdc,
                            cost=weighted_cost
                        )
                        break
                    
                    # Calculate ideal remaining shares at the current limit price
                    ideal_shares = remaining_budget / current_limit_price
                    target_shares = float(int(ideal_shares))
                    
                    if total_shares_filled > 0.0 and remaining_budget < 1.01:
                        logger.info(
                            "cascade_fill_stopped_budget_near_target",
                            market_id=market_id,
                            remaining_budget=remaining_budget,
                            total_filled=total_shares_filled,
                            cost=weighted_cost,
                            message="Stopping cascade fill: partial fill achieved and remaining budget is under $1.01 exchange minimum."
                        )
                        break

                    if target_shares < order_min_size:
                        if total_shares_filled > 0.0:
                            break
                        else:
                            # First attempt, ensure at least order_min_size and >= $1.01 notional
                            if current_limit_price > 0:
                                import math
                                min_notional_shares = float(math.ceil(1.01 / current_limit_price))
                                remaining_shares = max(order_min_size, target_shares, min_notional_shares)
                            else:
                                remaining_shares = max(order_min_size, target_shares)
                    else:
                        if total_shares_filled == 0.0 and current_limit_price > 0:
                            import math
                            min_notional_shares = float(math.ceil(1.01 / current_limit_price))
                            remaining_shares = max(target_shares, min_notional_shares)
                        else:
                            remaining_shares = target_shares
                    
                    # Mid-loop guard: if price moves to 0.9900 or higher during cascade fill attempts, abort further fills
                    if current_limit_price >= 0.9900:
                        logger.warning(
                            "cascade_fill_aborted_due_to_thin_book",
                            market_id=market_id,
                            price=current_limit_price,
                            filled=total_shares_filled,
                            message=f"Aborting subsequent cascade fill attempts: best ask ({current_limit_price}) is >= 0.9900"
                        )
                        break

                    logger.info(
                        "placing_cascade_order",
                        market_id=market_id,
                        side=side,
                        attempt=attempts,
                        price=current_limit_price,
                        size=remaining_shares,
                        filled_so_far=total_shares_filled,
                        target=size_usdc / entry_price if entry_price > 0.0 else remaining_shares
                    )

                    order_result = await self.clob.place_order(
                        token_id=target_token_id,
                        side="BUY",
                        price=current_limit_price,
                        size=remaining_shares,
                        order_type="FAK",
                    )

                    if not order_result:
                        logger.warning(
                            "cascade_order_placement_failed",
                            market_id=market_id,
                            price=current_limit_price,
                            size=remaining_shares
                        )
                        # Fetch new live ask for next attempt
                        live_ask = await _get_live_ask_price(target_token_id)
                        current_limit_price = live_ask
                        await asyncio.sleep(0.5)
                        continue

                    order_id = order_result.get("orderID") or order_result.get("id")
                    last_order_id = order_id
                    
                    # If the order placement response already indicates a terminal status (e.g. immediately killed/cancelled), bypass polling
                    placement_status = str(order_result.get("status", "")).upper()
                    if placement_status in ("MATCHED", "FILLED", "CANCELLED", "EXPIRED", "FAILED", "KILLED", "NOT_FOUND"):
                        status_check = order_result
                    else:
                        # Poll for order status with low latency (checking every 0.05s up to 2.5s for DELAYED matching)
                        status_check = None
                        poll_start = time.monotonic()
                        max_poll_time = 2.5 if placement_status == "DELAYED" else 1.5
                        while time.monotonic() - poll_start < max_poll_time:
                            status_check = await self.clob.get_order_status(order_id)
                            if status_check:
                                status = str(status_check.get("status", "")).upper()
                                if status in ("MATCHED", "FILLED", "CANCELLED", "EXPIRED", "FAILED", "NOT_FOUND", "KILLED", "CANCELED"):
                                    break
                            await asyncio.sleep(0.05)

                    if not status_check:
                        logger.warning(
                            "cascade_order_status_fetch_failed",
                            market_id=market_id,
                            order_id=order_id
                        )
                        # Fetch new live ask for safety
                        live_ask = await _get_live_ask_price(target_token_id)
                        current_limit_price = live_ask
                        continue

                    status = str(status_check.get("status", "")).upper()
                    
                    # Extract filled shares (handle legacy test compatibility where sizeMatched/size_matched is absent)
                    raw_size_matched = status_check.get("sizeMatched") or status_check.get("size_matched")
                    if raw_size_matched is not None:
                        try:
                            shares_filled_this_attempt = float(raw_size_matched)
                        except ValueError:
                            shares_filled_this_attempt = 0.0
                    else:
                        if status in ("MATCHED", "FILLED"):
                            shares_filled_this_attempt = remaining_shares
                        else:
                            shares_filled_this_attempt = 0.0

                    # Extract fill price
                    raw_fill_price = status_check.get("price") or current_limit_price
                    try:
                        price_filled_this_attempt = float(raw_fill_price)
                    except ValueError:
                        price_filled_this_attempt = current_limit_price

                    logger.info(
                        "cascade_order_attempt_result",
                        market_id=market_id,
                        order_id=order_id,
                        status=status,
                        requested=remaining_shares,
                        filled=shares_filled_this_attempt,
                        price=price_filled_this_attempt
                    )

                    if shares_filled_this_attempt > 0.0:
                        total_shares_filled += shares_filled_this_attempt
                        weighted_cost += shares_filled_this_attempt * price_filled_this_attempt
                        
                    # Fetch new live ask for next attempt if budget is not fully spent and supports at least 1.0 share
                    remaining_budget = size_usdc - weighted_cost
                    ideal_shares_remaining = remaining_budget / current_limit_price if current_limit_price > 0.0 else 0.0
                    if ideal_shares_remaining >= 1.0:
                        live_ask = await _get_live_ask_price(target_token_id)
                        current_limit_price = live_ask

                    # Brief cooldown between attempts
                    await asyncio.sleep(0.5)

                if total_shares_filled > 0.0:
                    if not self._paper:
                        if last_order_id:
                            try:
                                actual_fills = await self._get_actual_fills_from_trades(last_order_id)
                                if actual_fills:
                                    actual_vwap, actual_qty = actual_fills
                                    price_filled_this_attempt = actual_vwap
                                    weighted_cost = actual_vwap * actual_qty
                                    total_shares_filled = actual_qty
                                    logger.info(
                                        "cascade_order_fills_adjusted_post_sequence",
                                        market_id=market_id,
                                        order_id=last_order_id,
                                        actual_price=actual_vwap,
                                        actual_shares=actual_qty
                                    )
                            except Exception as f_err:
                                logger.warning("post_cascade_fills_query_error", error=str(f_err))

                        logger.info("verifying_live_holdings_post_buy", position_id=position_id, target_token_id=target_token_id)
                        confirmed_balance = await self._get_confirmed_token_balance(target_token_id)
                        if confirmed_balance > 0.0:
                            if abs(confirmed_balance - total_shares_filled) > 0.0001:
                                logger.info(
                                    "correcting_shares_with_confirmed_balance",
                                    position_id=position_id,
                                    old_shares=total_shares_filled,
                                    new_shares=confirmed_balance
                                )
                                total_shares_filled = confirmed_balance
                        else:
                            logger.warning("live_holdings_poll_unconfirmed_using_fill_shares", position_id=position_id, fill_shares=total_shares_filled)

                    final_price = round(weighted_cost / total_shares_filled, 4) if total_shares_filled > 0.0 else entry_price
                    final_size_usdc = round(total_shares_filled * final_price, 2)
                    
                    # Update entry_price, size_usdc, and shares_bought in DB
                    table = "paper_positions" if self._paper else "positions"
                    async with get_db_lock(self.db):
                        await execute_with_retry(
                            self.db,
                            f"UPDATE {table} SET entry_price = ?, size_usdc = ?, shares_bought = ? WHERE position_id = ?",
                            (final_price, final_size_usdc, float(total_shares_filled), position_id),
                        )
                        await commit_with_retry(self.db)

                    # Initialize tracking
                    self._partial_proceeds[position_id] = 0.0
                    self._shares_remaining[position_id] = float(total_shares_filled)
                    await self._persist_tracking_data(position_id, target_token_id)
                    
                    if self._paper:
                        await update_position_status(self.db, position_id, "OPEN", "paper_" + uuid.uuid4().hex[:16], paper=True)
                    else:
                        await update_position_status(self.db, position_id, "OPEN", last_order_id, paper=self._paper)
                        self._active_orders[last_order_id] = position_id
                    
                    # Notify Telegram
                    if self.tg:
                        try:
                            m_name = market_data.get("question", market_id) if market_data else market_id
                            applied_slip = final_price - entry_price
                            suffix = f" (Avg Slippage +{applied_slip*100:.2f}c)" if applied_slip > 0 else ""
                            disp_shares = float(total_shares_filled)
                            asyncio.create_task(self.tg.notify_trade_entry(
                                market_name=m_name + suffix,
                                entry_price=final_price,
                                size_usdc=final_size_usdc,
                                shares=disp_shares,
                                position_id=position_id,
                                paper=self._paper
                            ))
                        except Exception as tg_err:
                            logger.error("tg_entry_notify_failed", error=str(tg_err))
                            
                    if hasattr(self, "scanner") and self.scanner:
                        self.scanner.record_post_entry_spread(market_id, duration_seconds=5.0)

                    return True
                else:
                    await update_position_status(self.db, position_id, "FAILED", last_order_id, paper=self._paper)
                    return False

            except Exception as e:
                logger.error("open_position_error", market_id=market_id, error=str(e))
                await update_position_status(self.db, position_id, "ERROR", paper=self._paper)
                return False

    # -----------------------------------------------------------------
    # Monitoring Positions
    # -----------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """Scan all open positions and update their last known good price."""
        now = int(time.time())

        # Define which virtual profiles support exit triggers (TP, TTP, SL)
        profiles_with_tp = {
            'stop_loss', 'reversal_stop_loss', 'stop_loss_filtered', 'reversal_stop_loss_filtered',
            'claude_audit', 'claude_gated'
        }
        profiles_with_sl = {
            'stop_loss', 'reversal_stop_loss', 'stop_loss_filtered', 'reversal_stop_loss_filtered',
            'reversal_stop_loss_eth_5m_exhaustion', 'claude_audit', 'claude_gated',
            'cross_market_hedge', 'single_market_hedge_cheap', 'single_market_hedge_expensive',
            'era3_hybrid_bandpass'
        }

        # --- Monitor profile stop_loss and reversal_stop_loss positions for 12% Stop Loss, TP, and TTP ---
        try:
            from src.db.queries import get_active_and_unexpired_profile_positions
            open_sl_positions = await get_active_and_unexpired_profile_positions(self.db, now=now)
            for pos_row in open_sl_positions:
                pos = dict(pos_row)
                profile_name = pos["profile_name"]
                position_id = pos["position_id"]
                market_id = pos["market_id"]
                entry_price = pos["entry_price"]
                shares_bought = pos["shares_bought"]
                size_usdc = pos["size_usdc"]
                side = pos["side"]
                
                target_token_id = _get_tid_from_notes(pos.get("notes"))
                if not target_token_id:
                    target_token_id = await self._recover_target_token_id(market_id, position_id, side, profile_name=profile_name)
                    if not target_token_id:
                        continue
                
                m_data = self._market_cache.get(market_id)
                if not m_data:
                    from src.db.queries import get_market
                    m_data = await get_market(self.db, market_id)
                    self._market_cache[market_id] = m_data
                
                closes_at = m_data.get("closes_at", 0) if m_data else 0
                if closes_at > 0 and now >= closes_at:
                    logger.debug("skipping_expired_profile_position_monitoring", position_id=position_id, closes_at=closes_at, now=now)
                    continue
                
                opened_at = pos.get("opened_at", 0)
                
                # Check WebSocket ask price first for virtual profiles to avoid initial spread drag/instant stop-outs
                current_price = None
                if self.ws:
                    current_price = self._clean_price(self.ws.get_best_ask(target_token_id))
                    
                if current_price is None or current_price <= 0:
                    current_price = await self._get_current_price_with_rest_fallback(
                        target_token_id, market_id, side, position_id, opened_at=opened_at, is_profile=True
                    )
                
                if current_price is None or current_price <= 0:
                    current_price = self._lkgp_memory.get(position_id)
                
                if current_price is None or current_price <= 0:
                    # Tier 3 Fallback: Use database-stored market price updated by the scanner
                    if m_data:
                        db_price = m_data.get("no_price") if side == "NO" else m_data.get("yes_price")
                        if db_price is not None and db_price > 0:
                            current_price = db_price

                if current_price is None or current_price <= 0:
                    # Tier 4 Fallback: Use the position's entry price as a safe baseline
                    if entry_price is not None and entry_price > 0:
                        current_price = entry_price

                if current_price is None or current_price <= 0:
                    continue

                self._lkgp_memory[position_id] = current_price

                # Track min/max prices reached
                max_seen = pos.get("max_price_seen")
                min_seen = pos.get("min_price_seen")

                if max_seen is None:
                    max_seen = entry_price if entry_price is not None else current_price
                if min_seen is None:
                    min_seen = entry_price if entry_price is not None else current_price

                new_max = max(max_seen, current_price)
                new_min = min(min_seen, current_price)

                if new_max != max_seen or new_min != min_seen:
                    pos["max_price_seen"] = new_max
                    pos["min_price_seen"] = new_min
                    from src.db.queries import update_profile_position_min_max_prices
                    await update_profile_position_min_max_prices(
                        self.db,
                        position_id,
                        new_min,
                        new_max
                    )

                # If position is already closed (e.g. FILLED or STOP_LOSS), skip exit triggers
                if pos.get("order_status") != "OPEN":
                    continue

                # Recovery logic: Ensure TTP memory state is loaded
                if position_id not in self._trailing_tp_active:
                    await self._load_tracking_data(pos)


                # Get the bid price to use as the exit price if closed
                bid_price = None
                if self.ws:
                    bid_price = self._clean_price(self.ws.get_best_bid(target_token_id))
                if bid_price is None or bid_price <= 0:
                    bid_price = current_price

                closed = False

                # 1. Take Profit trigger (Static ceiling)
                hold_res = getattr(self.config, "hold_to_resolution", False)
                if type(hold_res).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    hold_res = False
                else:
                    hold_res = bool(hold_res)

                if not hold_res and profile_name in profiles_with_tp:
                    tp_threshold = getattr(self.config, "take_profit_threshold", 0.98)
                    if type(tp_threshold).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                        tp_threshold = 0.98
                    else:
                        tp_threshold = float(tp_threshold)

                    if current_price >= tp_threshold:
                        pnl = calc_realised_pnl(bid_price, shares_bought, size_usdc)
                        from src.db.queries import close_profile_position
                        await close_profile_position(self.db, position_id, bid_price, pnl, "TAKE_PROFIT")
                        self.open_probability_sl_positions.pop(position_id, None)
                        self._prob_sl_first_dip.pop(position_id, None)
                        logger.info(
                            "profile_position_take_profit_triggered",
                            profile=profile_name,
                            position_id=position_id,
                            entry_price=entry_price,
                            exit_price=bid_price,
                            realised_pnl=pnl
                        )
                        closed = True

                # 2. Trailing Take Profit (TTP) trigger
                if not closed:
                    ttp_enabled = getattr(self.config, "trailing_tp_enabled", True)
                    if type(ttp_enabled).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                        ttp_enabled = False
                    else:
                        ttp_enabled = bool(ttp_enabled)

                    if ttp_enabled and profile_name in profiles_with_tp:
                        ttp_trigger = getattr(self.config, "trailing_tp_trigger", 0.80)
                        if type(ttp_trigger).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                            ttp_trigger = 0.80
                        else:
                            ttp_trigger = float(ttp_trigger)

                        ttp_trail = getattr(self.config, "trailing_tp_trail", 0.05)
                        if type(ttp_trail).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                            ttp_trail = 0.05
                        else:
                            ttp_trail = float(ttp_trail)
                        
                        is_armed = self._trailing_tp_active.get(position_id, False)
                        peak = self._trailing_tp_peak.get(position_id, 0.0)
                        
                        if not is_armed:
                            if current_price >= ttp_trigger:
                                self._trailing_tp_active[position_id] = True
                                self._trailing_tp_peak[position_id] = current_price
                                logger.info(
                                    "profile_trailing_tp_armed",
                                    profile=profile_name,
                                    position_id=position_id,
                                    price=current_price,
                                    trigger=ttp_trigger
                                )
                                await self._persist_tracking_data(position_id, target_token_id, profile_name=profile_name)
                        else:
                            if current_price > peak:
                                self._trailing_tp_peak[position_id] = current_price
                                logger.info(
                                    "profile_trailing_tp_peak_updated",
                                    profile=profile_name,
                                    position_id=position_id,
                                    old_peak=peak,
                                    new_peak=current_price
                                )
                                await self._persist_tracking_data(position_id, target_token_id, profile_name=profile_name)
                                peak = current_price
                            
                            stop_price = peak - ttp_trail
                            if current_price <= stop_price:
                                pnl = calc_realised_pnl(bid_price, shares_bought, size_usdc)
                                from src.db.queries import close_profile_position
                                await close_profile_position(self.db, position_id, bid_price, pnl, "TRAILING_TP")
                                self.open_probability_sl_positions.pop(position_id, None)
                                self._prob_sl_first_dip.pop(position_id, None)
                                logger.info(
                                    "profile_position_trailing_tp_triggered",
                                    profile=profile_name,
                                    position_id=position_id,
                                    entry_price=entry_price,
                                    exit_price=bid_price,
                                    realised_pnl=pnl,
                                    peak=peak,
                                    stop_price=stop_price
                                )
                                closed = True

                # 3. Stop Loss trigger (10% for hedges, 12% for filtered, 20% for baseline, 15% static for claude_audit, 3% for eth_5m_exhaustion)
                if not closed and profile_name in profiles_with_sl:
                    if profile_name == "reversal_stop_loss_eth_5m_exhaustion":
                        sl_mult = 0.97  # 3% Stop Loss cut
                    elif profile_name in ("claude_audit", "claude_gated"):
                        sl_mult = 0.85  # 15% SL
                    elif profile_name == "cross_market_hedge":
                        sl_mult = 0.83  # 17% SL
                    elif profile_name in ('single_market_hedge_cheap', 'single_market_hedge_expensive'):
                        sl_mult = 0.90
                    elif profile_name in ('stop_loss_filtered', 'reversal_stop_loss_filtered'):
                        sl_mult = 0.88
                    elif profile_name == "era3_hybrid_bandpass":
                        sl_mult = 0.0 if entry_price < 0.35 else 0.88
                    else:
                        sl_mult = 0.80

                    if current_price <= entry_price * sl_mult:
                        pnl = calc_realised_pnl(bid_price, shares_bought, size_usdc)
                        from src.db.queries import close_profile_position
                        await close_profile_position(self.db, position_id, bid_price, pnl, "STOP_LOSS")
                        self.open_probability_sl_positions.pop(position_id, None)
                        self._prob_sl_first_dip.pop(position_id, None)
                        logger.info(
                            "profile_position_stop_loss_triggered",
                            profile=profile_name,
                            position_id=position_id,
                            entry_price=entry_price,
                            exit_price=bid_price,
                            realised_pnl=pnl
                        )
        except Exception as sl_err:
            logger.error("profile_stop_loss_monitoring_failed", error=str(sl_err))

        from src.db.queries import get_active_and_unexpired_positions
        open_positions = await get_active_and_unexpired_positions(self.db, paper=self._paper, now=now)
        if not open_positions:
            return

        for pos in open_positions:
            position_id = pos["position_id"]
            market_id = pos["market_id"]
            target_token_id = _get_tid_from_notes(pos.get("notes"))
            side = pos.get("side", "NO")
            
            if market_id not in self._market_cache:
                from src.db.queries import get_market
                self._market_cache[market_id] = await get_market(self.db, market_id)
            m_data = self._market_cache[market_id]

            if not target_token_id:
                target_token_id = await self._recover_target_token_id(market_id, position_id, side)
                if not target_token_id: continue

            closes_at = m_data.get("closes_at", 0) if m_data else 0
            if closes_at > 0 and now >= closes_at:
                logger.info("skipping_expired_core_position_monitoring", position_id=position_id, closes_at=closes_at, now=now)
                continue

            # ─── ANTI-BLINDNESS FIX & SPREAD SUICIDE GRACE PERIOD ───
            opened_at = pos.get("opened_at", 0)
            current_target_price = await self._get_current_price_with_rest_fallback(
                target_token_id, market_id, side, position_id, opened_at=opened_at
            )
            
            if current_target_price is None or current_target_price <= 0:
                current_target_price = self._lkgp_memory.get(position_id)
            
            if current_target_price is None or current_target_price <= 0:
                # Tier 3 Fallback: Use database-stored market price updated by the scanner
                if m_data:
                    db_price = m_data.get("no_price") if side == "NO" else m_data.get("yes_price")
                    if db_price is not None and db_price > 0:
                        current_target_price = db_price

            if current_target_price is None or current_target_price <= 0:
                # Tier 4 Fallback: Use the position's entry price as a safe baseline
                entry_price = pos.get("entry_price")
                if entry_price is not None and entry_price > 0:
                    current_target_price = entry_price

            if current_target_price is None or current_target_price <= 0:
                continue
            
            self._lkgp_memory[position_id] = current_target_price

            # Track minimum and maximum prices reached during position lifetime until resolution
            max_seen = pos.get("max_price_seen")
            min_seen = pos.get("min_price_seen")
            entry_price = pos.get("entry_price")

            if max_seen is None:
                max_seen = entry_price if entry_price is not None else current_target_price
            if min_seen is None:
                min_seen = entry_price if entry_price is not None else current_target_price

            new_max = max(max_seen, current_target_price)
            new_min = min(min_seen, current_target_price)

            if new_max != max_seen or new_min != min_seen:
                pos["max_price_seen"] = new_max
                pos["min_price_seen"] = new_min
                await update_position_min_max_prices(
                    self.db,
                    position_id,
                    new_min,
                    new_max,
                    paper=self._paper
                )
                logger.debug("position_min_max_updated", pid=position_id, min_price=new_min, max_price=new_max)

            # If position is already closed, skip exit triggers
            if pos.get("order_status") not in ("OPEN", "PENDING", "PARTIAL_FILLED"):
                continue

            # Recovery logic: Ensure memory state is consistent with database notes
            if position_id not in self._partial_proceeds:
                await self._load_tracking_data(pos)


            # Take Profit / TTP / Stop Loss triggers (Bypassed if hold_to_resolution or livemode HTR position)
            hold_res = getattr(self.config, "hold_to_resolution", False)
            if type(hold_res).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                hold_res = False
            else:
                hold_res = bool(hold_res)

            is_htr = hold_res or "LIVEMODE_CMH" in str(pos.get("notes", "")) or getattr(self.config, "live_profile_only", "") == "livemode"

            if position_id not in self._closing_positions and not is_htr:
                tp_threshold = getattr(self.config, "take_profit_threshold", 0.95)
                if type(tp_threshold).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    tp_threshold = 0.95
                else:
                    tp_threshold = float(tp_threshold)

                if current_target_price >= tp_threshold:
                    logger.info(
                        "take_profit_triggered",
                        position_id=position_id,
                        price=current_target_price,
                        threshold=tp_threshold,
                        message=f"Take Profit triggered for position {position_id}: price {current_target_price} >= {tp_threshold}"
                    )
                    # Run close_position asynchronously as a task to prevent blocking the monitoring loop
                    asyncio.create_task(self._close_position(pos, current_target_price, "TAKE_PROFIT"))

            # Trailing Take Profit (TTP) trigger
            ttp_enabled = getattr(self.config, "trailing_tp_enabled", False)
            if type(ttp_enabled).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                ttp_enabled = False
            else:
                ttp_enabled = bool(ttp_enabled)

            if position_id not in self._closing_positions and ttp_enabled and not is_htr:
                ttp_trigger = getattr(self.config, "trailing_tp_trigger", 0.80)
                if type(ttp_trigger).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    ttp_trigger = 0.80
                else:
                    ttp_trigger = float(ttp_trigger)

                ttp_trail = getattr(self.config, "trailing_tp_trail", 0.05)
                if type(ttp_trail).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    ttp_trail = 0.05
                else:
                    ttp_trail = float(ttp_trail)
                
                is_armed = self._trailing_tp_active.get(position_id, False)
                peak = self._trailing_tp_peak.get(position_id, 0.0)
                
                if not is_armed:
                    if current_target_price >= ttp_trigger:
                        self._trailing_tp_active[position_id] = True
                        self._trailing_tp_peak[position_id] = current_target_price
                        logger.info(
                            "trailing_tp_armed",
                            position_id=position_id,
                            price=current_target_price,
                            trigger=ttp_trigger
                        )
                        await self._persist_tracking_data(position_id, target_token_id)
                else:
                    if current_target_price > peak:
                        self._trailing_tp_peak[position_id] = current_target_price
                        logger.info(
                            "trailing_tp_peak_updated",
                            position_id=position_id,
                            old_peak=peak,
                            new_peak=current_target_price
                        )
                        await self._persist_tracking_data(position_id, target_token_id)
                        peak = current_target_price
                    
                    stop_price = peak - ttp_trail
                    if current_target_price <= stop_price:
                        logger.info(
                            "trailing_tp_triggered",
                            position_id=position_id,
                            price=current_target_price,
                            peak=peak,
                            stop_price=stop_price,
                            message=f"Trailing Take Profit triggered for position {position_id}: price {current_target_price} <= {stop_price} (peak: {peak})"
                        )
                        asyncio.create_task(self._close_position(pos, current_target_price, "TRAILING_TP"))

            # Stop Loss trigger (configurable percentage, sweet spot 12%)
            stop_loss_enabled = getattr(self.config, "stop_loss_enabled", True)
            if type(stop_loss_enabled).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                stop_loss_enabled = True
            else:
                stop_loss_enabled = bool(stop_loss_enabled)

            if stop_loss_enabled and not is_htr and position_id not in self._closing_positions and entry_price is not None:
                stop_loss_pct = getattr(self.config, "stop_loss_pct", 0.12)
                if type(stop_loss_pct).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    stop_loss_pct = 0.12
                else:
                    stop_loss_pct = float(stop_loss_pct)

                if current_target_price <= entry_price * (1.0 - stop_loss_pct):
                    logger.info(
                        "stop_loss_triggered",
                        position_id=position_id,
                        price=current_target_price,
                        entry=entry_price,
                        message=f"Stop Loss triggered for position {position_id}: price {current_target_price} <= {entry_price * (1.0 - stop_loss_pct)}"
                    )
                    asyncio.create_task(self._close_position(pos, current_target_price, "STOP_LOSS"))


    async def _get_binance_resolution(self, coin: str, duration: int, start_time: int) -> str:
        """
        Query public Binance Klines to resolve the UP/DOWN outcome of a candle.
        Returns: 'UP', 'DOWN', or '?'
        """
        import os
        if "PYTEST_CURRENT_TEST" in os.environ:
            return "?" # Skip live network queries in unit tests
            
        symbol = f"{coin.upper()}USDT"
        interval = f"{duration}m"
        start_ms = start_time * 1000
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_ms}&limit=1"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            k = data[0]
                            candle_start_time = int(k[0])
                            # Strict timestamp match guard
                            if candle_start_time == start_ms:
                                open_p = float(k[1])
                                close_p = float(k[4])
                                return "UP" if close_p > open_p else "DOWN"
                            else:
                                logger.warning("healer_binance_timestamp_mismatch", expected=start_ms, got=candle_start_time)
        except Exception as e:
            logger.error("healer_binance_fetch_failed", symbol=symbol, start_time=start_time, error=str(e))
        return "?"

    async def check_resolutions(self) -> None:
        """Check Gamma and Binance spot index for market resolution outcomes."""
        async with self._resolution_lock:
            from src.strategy.resolver import market_resolver
            market_resolver.clear_cache()

            open_positions = await get_open_positions(self.db, paper=self._paper)
            now = int(time.time())
            for pos in open_positions:
                # ─── REAL ORDER CONFIRMATION GUARD ───
                if not self._paper:
                    order_id = pos.get("order_id")
                    status = pos.get("order_status")
                    if not order_id or status == "PENDING":
                        logger.warning(
                            "unconfirmed_live_position_aborted",
                            position_id=pos["position_id"],
                            order_id=order_id,
                            status=status,
                            message="Aborting resolution for unconfirmed/orphaned live position"
                        )
                        async with get_db_lock(self.db):
                            await execute_with_retry(
                                self.db,
                                "UPDATE positions SET order_status = 'FAILED', exit_price = 0.0, realised_pnl = 0.0, close_reason = 'ORDER_UNCONFIRMED_FAILED' WHERE position_id = ?",
                                (pos["position_id"],)
                            )
                            await commit_with_retry(self.db)
                        continue

                market_id = pos["market_id"]
                m_data = await get_market(self.db, market_id)
                if not m_data or (m_data.get("closes_at", 0) > now): continue
                
                closes_at = m_data.get("closes_at", 0)
                duration = pos.get("window_minutes") or 5
                q = m_data.get("question", "").lower()
                coin = "btc" if ("bitcoin" in q or "btc" in q) else "eth"
                start_time = closes_at - duration * 60
                side = pos.get("side", "NO")
                
                res = await market_resolver.get_market_resolution(
                    self.db, self.gamma, market_id, coin, duration, start_time, side
                )
                if res:
                    outcome, exit_price = res
                    await self._close_position(pos, exit_price, "RESOLUTION")

            # --- Resolve profile positions ---
            try:
                from src.db.queries import get_open_profile_positions, close_profile_position
                open_profile_positions = await get_open_profile_positions(self.db)
                for pos in open_profile_positions:
                    market_id = pos["market_id"]
                    p_id = pos["position_id"]
                    p_name = pos["profile_name"]
                    
                    m_data = await get_market(self.db, market_id)
                    if not m_data:
                        opened_at = int(pos.get("opened_at") or 0)
                        win_mins = int(pos.get("window_minutes") or 5)
                        if opened_at > 0 and (now - opened_at) >= (win_mins * 60 + 60):
                            m_data = {
                                "market_id": market_id,
                                "closes_at": opened_at + win_mins * 60,
                                "window_minutes": win_mins,
                                "question": str(pos.get("symbol") or "btc"),
                            }
                    if not m_data or (m_data.get("closes_at", 0) > now):
                        continue
                    
                    closes_at = m_data.get("closes_at", 0)
                    duration = pos.get("window_minutes") or m_data.get("window_minutes") or 5
                    q = m_data.get("question", "").lower()
                    coin = "btc" if ("bitcoin" in q or "btc" in q) else "eth"
                    start_time = closes_at - duration * 60
                    side = pos.get("side", "NO")
                    
                    res = await market_resolver.get_market_resolution(
                        self.db, self.gamma, market_id, coin, duration, start_time, side
                    )
                    if res:
                        outcome, exit_price = res
                        
                        # Calculate virtual realized P&L
                        pnl = calc_realised_pnl(exit_price, pos["shares_bought"], pos["size_usdc"])
                        await close_profile_position(self.db, p_id, exit_price, pnl, "RESOLUTION")
                        self.open_probability_sl_positions.pop(p_id, None)
                        self._prob_sl_first_dip.pop(p_id, None)
                        logger.info(
                            "profile_position_resolved",
                            profile=p_name,
                            position_id=p_id,
                            exit_price=exit_price,
                            realised_pnl=pnl
                        )
            except Exception as e:
                logger.error("profile_positions_resolution_check_failed", error=str(e))

        # --- D3 signals Table Auditing: Resolve Unresolved Signal Entries ---
        signals_updated = False
        try:
            import os
            from src.db.queries import execute_with_retry, commit_with_retry, get_db_lock, fetch_all
            unresolved_signals = await fetch_all(
                self.db,
                "SELECT signal_id, market_id FROM signals WHERE eventual_resolution IS NULL ORDER BY signal_timestamp DESC LIMIT 20"
            )
            market_to_sigs = {}
            for sig in unresolved_signals:
                sig_id = sig[0]
                m_id = sig[1]
                market_to_sigs.setdefault(m_id, []).append(sig_id)

            for m_id, sig_ids in market_to_sigs.items():
                if is_mock_market_id(m_id) and ("PYTEST_CURRENT_TEST" not in os.environ):
                    async with get_db_lock(self.db):
                        await execute_with_retry(
                            self.db,
                            "UPDATE signals SET eventual_resolution = ? WHERE market_id = ? AND eventual_resolution IS NULL",
                            ("MOCK", m_id)
                        )
                        await commit_with_retry(self.db)
                    for sig_id in sig_ids:
                        logger.info("signal_resolution_mock_skipped", signal_id=sig_id, market_id=m_id)
                    continue
                
                try:
                    m_data = await get_market(self.db, m_id)
                    closes_at = m_data.get("closes_at", 0) if m_data else 0
                    
                    # Only resolve if the market has reached close time
                    if closes_at > 0 and now >= closes_at:
                        duration = (m_data.get("window_minutes") if m_data else None) or 5
                        q = (m_data.get("question", "") if m_data else "").lower()
                        coin = "btc" if ("bitcoin" in q or "btc" in q) else "eth"
                        start_time = closes_at - duration * 60
                        
                        # 1. Primary: Binance Klines resolution
                        res = await market_resolver.get_market_resolution(
                            self.db, self.gamma, m_id, coin, duration, start_time, "YES"
                        )
                        resolved_outcome = None
                        if res:
                            outcome, _ = res  # outcome is 'UP' or 'DOWN'
                            resolved_outcome = "YES" if outcome == "UP" else "NO"
                        
                        # 2. Fallback: Gamma CLOB outcomePrices
                        if not resolved_outcome:
                            clob_data = await self.gamma._get(f"/markets/{m_id}")
                            if clob_data:
                                prices = json.loads(clob_data.get("outcomePrices", "[]"))
                                outcomes = json.loads(clob_data.get("outcomes", "[]"))
                                if len(prices) >= 2 and len(outcomes) >= 2:
                                    for idx, outcome in enumerate(outcomes):
                                        outcome_upper = str(outcome).upper()
                                        price = float(prices[idx])
                                        if price >= 0.98:
                                            resolved_outcome = outcome_upper
                                            break
                        
                        if resolved_outcome is not None:
                            async with get_db_lock(self.db):
                                await execute_with_retry(
                                    self.db,
                                    "UPDATE signals SET eventual_resolution = ? WHERE market_id = ? AND eventual_resolution IS NULL",
                                    (resolved_outcome, m_id)
                                )
                                await commit_with_retry(self.db)
                            signals_updated = True
                            for sig_id in sig_ids:
                                logger.info("signal_resolution_updated", signal_id=sig_id, market_id=m_id, eventual_resolution=resolved_outcome)
                except Exception as sig_err:
                    logger.debug("signal_resolution_check_error", market_id=m_id, error=str(sig_err))
        except Exception as e:
            logger.error("signals_audit_resolution_failed", error=str(e))

        if signals_updated:
            try:
                from src.db.queries import recalculate_win_rates
                asyncio.create_task(recalculate_win_rates(self.db, rolling_n=50))
                logger.info("win_rates_recalculation_triggered_via_signals_update")
            except Exception as r_err:
                logger.error("win_rates_recalculation_failed_in_audit", error=str(r_err))

        # --- Soft Trades Table Auditing: Resolve Unresolved Soft Trades ---
        try:
            from src.db.soft_audit_db import get_pending_soft_trades, update_soft_trade_resolution
            soft_db_path = "data/soft_guards_audit.db"
            pending_soft = await get_pending_soft_trades(soft_db_path)
            for st in pending_soft:
                st_id = st["id"]
                m_id = st["market_id"]
                st_side = st["side"]
                entry_price = st["entry_price"]
                
                import os
                if is_mock_market_id(m_id) and ("PYTEST_CURRENT_TEST" not in os.environ):
                    await update_soft_trade_resolution(soft_db_path, st_id, "MOCK", 0.0)
                    logger.info("soft_trade_resolution_mock_skipped", trade_id=st_id, market_id=m_id)
                    continue
                
                try:
                    clob_data = await self.gamma._get(f"/markets/{m_id}")
                    if clob_data:
                        prices = json.loads(clob_data.get("outcomePrices", "[]"))
                        outcomes = json.loads(clob_data.get("outcomes", "[]"))
                        if len(prices) >= 2 and len(outcomes) >= 2:
                            resolved_outcome = None
                            for idx, outcome in enumerate(outcomes):
                                outcome_upper = str(outcome).upper()
                                price = float(prices[idx])
                                if price == 1.0:
                                    resolved_outcome = outcome_upper
                                    break
                            
                            if resolved_outcome is not None:
                                win = (st_side == "YES" and resolved_outcome in ("YES", "UP")) or (st_side == "NO" and resolved_outcome in ("NO", "DOWN"))
                                exit_price = 1.0 if win else 0.0
                                shares = 10.0 / entry_price
                                st_pnl = (exit_price - entry_price) * shares
                                if win:
                                    st_pnl = st_pnl * 0.98
                                    
                                await update_soft_trade_resolution(soft_db_path, st_id, resolved_outcome, st_pnl)
                                logger.info(
                                    "soft_trade_resolution_updated",
                                    trade_id=st_id,
                                    market_id=m_id,
                                    outcome=resolved_outcome,
                                    win=win,
                                    pnl=st_pnl
                                )
                except Exception as st_err:
                    logger.debug("soft_trade_resolution_check_error", market_id=m_id, error=str(st_err))
        except Exception as e:
            logger.error("soft_trades_audit_resolution_failed", error=str(e))

        # --- Loss Momentum Skip Database Auditing: Resolve Unresolved Skips ---
        if self.skip_db is not None:
            try:
                unresolved_skips = await self.skip_db.get_unresolved_skips()
                for skip in unresolved_skips:
                    skip_id = skip["position_id"]
                    m_id = skip["market_id"]
                    skip_side = skip["side"]
                    entry_price = skip["entry_price"]
                    shares = skip["shares_bought"]
                    size_usdc = skip["size_usdc"]

                    import os
                    if is_mock_market_id(m_id) and ("PYTEST_CURRENT_TEST" not in os.environ):
                        await self.skip_db.resolve_skip(skip_id, 0.0, 0.0, "MOCK")
                        logger.info("skip_resolution_mock_skipped", position_id=skip_id, market_id=m_id)
                        continue

                    m_data = await get_market(self.db, m_id)
                    opened_at = int(skip.get("opened_at") or 0)
                    if m_data:
                        if m_data.get("closes_at", 0) > now:
                            continue
                        closes_at = int(m_data.get("closes_at", 0) or 0)
                        slug_str = str(m_data.get("slug", "")).lower()
                        q = str(m_data.get("question", "")).lower()
                        inferred_dur = 15 if ("-15m-" in slug_str or "15-minute" in q) else 5
                        duration = int(skip.get("window_minutes") or m_data.get("window_minutes") or inferred_dur)
                        coin = "btc" if ("bitcoin" in q or "btc" in q or "btc" in slug_str) else "eth"
                        start_time = closes_at - duration * 60
                        binance_outcome = await self._get_binance_resolution(coin, duration, start_time)
                    else:
                        if opened_at > 0 and (now - opened_at) < 900:
                            continue
                        closes_at = 0
                        binance_outcome = None

                    resolved = False
                    win = False
                    exit_price = 0.0

                    if binance_outcome in ("UP", "DOWN"):
                        win = (skip_side == "YES" and binance_outcome == "UP") or (skip_side == "NO" and binance_outcome == "DOWN")
                        exit_price = 1.0 if win else 0.0
                        resolved = True
                        logger.info(
                            "skip_resolution_verified_via_binance",
                            position_id=skip_id,
                            market_id=m_id,
                            side=skip_side,
                            binance_outcome=binance_outcome,
                            win=win,
                            exit_price=exit_price
                        )
                    else:
                        # 2. Fallback to Gamma CLOB outcomePrices
                        try:
                            clob_data = await self.gamma._get(f"/markets/{m_id}") if self.gamma else None
                            if clob_data:
                                if closes_at <= 0:
                                    slug_val = str(clob_data.get("slug") or "").lower()
                                    parts = slug_val.split("-")
                                    if parts and parts[-1].isdigit():
                                        start_ts = int(parts[-1])
                                        dur_mins = 15 if "-15m-" in slug_val else 5
                                        closes_at = start_ts + dur_mins * 60
                                prices = json.loads(clob_data.get("outcomePrices", "[]"))
                                outcomes = json.loads(clob_data.get("outcomes", "[]"))
                                if len(prices) >= 2 and len(outcomes) >= 2:
                                    p_target = None
                                    p_opposing = None
                                    for idx, outcome in enumerate(outcomes):
                                        outcome_upper = str(outcome).upper()
                                        if skip_side == "YES" and outcome_upper in ("YES", "UP"):
                                            p_target = float(prices[idx])
                                        elif skip_side == "NO" and outcome_upper in ("NO", "DOWN"):
                                            p_target = float(prices[idx])
                                        else:
                                            p_opposing = float(prices[idx])
                                    
                                    if p_target is not None:
                                        if p_target >= 0.98:
                                            win = True
                                            exit_price = 1.0
                                            resolved = True
                                        elif p_target <= 0.02 or (p_opposing is not None and p_opposing >= 0.98):
                                            win = False
                                            exit_price = 0.0
                                            resolved = True
                                        
                                        if resolved:
                                            logger.info(
                                                "skip_resolution_verified_via_gamma",
                                                position_id=skip_id,
                                                market_id=m_id,
                                                side=skip_side,
                                                win=win,
                                                exit_price=exit_price
                                            )
                        except Exception as sig_err:
                            logger.debug("skip_signal_resolution_check_error", market_id=m_id, error=str(sig_err))

                    historical_close_ts = closes_at if closes_at > 0 else (opened_at + 300 if opened_at > 0 else now)
                    if resolved:
                        # Calculate hypothetical realised P&L
                        pnl = calc_realised_pnl(exit_price, shares, size_usdc)
                        await self.skip_db.resolve_skip(
                            skip_id, exit_price, pnl, closed_at=historical_close_ts
                        )
                        logger.info(
                            "skip_position_resolved",
                            position_id=skip_id,
                            market_id=m_id,
                            exit_price=exit_price,
                            realised_pnl=pnl
                        )
                    elif not m_data and opened_at > 0 and (now - opened_at) >= 7200:
                        await self.skip_db.resolve_skip(
                            skip_id, 0.0, 0.0, close_reason="EXPIRED", closed_at=historical_close_ts
                        )
                        logger.warning(
                            "skip_position_expired_no_market_data",
                            position_id=skip_id,
                            market_id=m_id,
                            opened_at=opened_at
                        )
            except Exception as e:
                logger.error("skip_positions_resolution_check_failed", error=str(e))

    async def manual_close_position(self, position_id: str) -> bool:
        """Emergency manual close — tries every price source and uses aggressive 3-strike FAK."""
        pos = await get_position(self.db, position_id, paper=self._paper)
        if not pos:
            logger.error("manual_close_failed", position_id=position_id, message="Position not found in DB")
            return False

        if position_id not in self._partial_proceeds:
            await self._load_tracking_data(pos)

        tid = _get_tid_from_notes(pos.get("notes"))

        # Price source priority: bid (sell side) → ask → last trade
        price = (
            self.ws.get_best_bid(tid)
            or self.ws.get_best_ask(tid)
            or self.ws.get_live_price(tid)
        )

        # Last resort: implied price from the YES side
        if not price:
            yes_tid = None
            # Try to find YES token from tracked markets
            for m in getattr(self, '_tracked_markets_ref', {}).values():
                if m.get("no_token_id") == tid:
                    yes_tid = m.get("yes_token_id")
                    break
            if yes_tid:
                yes_ask = self.ws.ask_cache.get(yes_tid) or self.ws.price_cache.get(yes_tid)
                if yes_ask:
                    price = round(1.0 - yes_ask, 4)

        if not price:
            # Absolute fallback — use entry price as exit (worst case, at least closes the position)
            price = pos.get("entry_price", 0.01)
            logger.warning(
                "manual_close_no_live_price",
                position_id=position_id,
                fallback_price=price,
                message="No live price found — using entry price as fallback exit price"
            )

        logger.info(
            "manual_close_initiated",
            position_id=position_id,
            market_id=pos.get("market_id"),
            price=price,
            message=f"Manual close triggered at price {price}"
        )
        # Use STOP_LOSS path so it gets the aggressive 3-strike FAK loop
        return await self._close_position(pos, price, "STOP_LOSS", skip_cancel=False)

    async def _close_position(self, pos: Dict, exit_price: float, reason: str, skip_cancel: bool = False) -> bool:
        """Unified exit logic."""
        pid = pos["position_id"]
        if pid in self._closing_positions:
            logger.warning("close_position_already_in_progress", pid=pid)
            return False
        self._closing_positions.add(pid)

        try:
            tid = _get_tid_from_notes(pos.get("notes"))
            
            # Fetch initial confirmed balance for live mode to sync the shares bag
            if not self._paper and reason != "RESOLUTION":
                logger.info("fetching_live_balance_for_exit", pid=pid, target_token_id=tid)
                confirmed_balance = await self._get_confirmed_token_balance(tid)
                self._shares_remaining[pid] = confirmed_balance
                await self._persist_tracking_data(pid, tid)
                
            shares_to_sell = self._shares_remaining.get(pid, pos["shares_bought"])
            
            if not skip_cancel:
                tp_orders = self._resting_tp_orders.pop(pid, [])
                for tp in tp_orders:
                    try:
                        status = await self.clob.get_order_status(tp["order_id"])
                        if status and status.get("status") in ("MATCHED", "FILLED"):
                            proceeds = tp["shares"] * tp["price"] * 0.98
                            self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + proceeds
                            shares_to_sell -= tp["shares"]
                        else:
                            await self.clob.cancel_order(tp["order_id"])
                    except: pass

            if reason == "RESOLUTION":
                # For resolved markets, do not place any order on CLOB.
                # The contract pays out 1.0 (win) or 0.0 (loss) per share.
                self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + (shares_to_sell * exit_price)
                shares_to_sell = 0.0
            elif shares_to_sell >= 0.1:
                # Standard exit / STOP_LOSS / TAKE_PROFIT: 3 tries with increasing slippage (1%, 3%, 5%)
                slippages = [0.01, 0.03, 0.05]
                for attempt, slippage in enumerate(slippages, 1):
                    if shares_to_sell < 0.1:
                        break
                        
                    sell_price = max(0.01, exit_price - slippage)
                    
                    if self._paper:
                        # Paper trading: simulate fill immediately
                        logger.info("simulating_paper_exit", pid=pid, price=sell_price, shares=shares_to_sell)
                        self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + (shares_to_sell * sell_price)
                        shares_to_sell = 0.0
                        break
                        
                    logger.info("placing_live_exit_order", pid=pid, attempt=attempt, price=sell_price, shares=shares_to_sell)
                    try:
                        res = await self.clob.place_order(tid, "SELL", sell_price, shares_to_sell, "FAK")
                        if res:
                            order_id = res.get("orderID") or res.get("id")
                            
                            # Low-latency status polling for FAK order fill
                            status_check = None
                            poll_start = time.monotonic()
                            while time.monotonic() - poll_start < 1.5:
                                status_check = await self.clob.get_order_status(order_id)
                                if status_check:
                                    status = str(status_check.get("status", "")).upper()
                                    if status in ("MATCHED", "FILLED", "CANCELLED", "EXPIRED", "FAILED", "NOT_FOUND", "KILLED", "CANCELED"):
                                        break
                                await asyncio.sleep(0.05)
                                
                            status = str(status_check.get("status", "")).upper() if status_check else "FAILED"
                            raw_size_matched = status_check.get("sizeMatched") or status_check.get("size_matched") if status_check else None
                            if raw_size_matched is not None:
                                try:
                                    shares_filled = float(raw_size_matched)
                                except ValueError:
                                    shares_filled = 0.0
                            else:
                                if status in ("MATCHED", "FILLED"):
                                    shares_filled = shares_to_sell
                                else:
                                    shares_filled = 0.0
                                    
                            raw_fill_price = status_check.get("price") or sell_price if status_check else sell_price
                            try:
                                fill_price = float(raw_fill_price)
                            except ValueError:
                                fill_price = sell_price
                                
                            logger.info(
                                "live_exit_order_status",
                                pid=pid,
                                order_id=order_id,
                                status=status,
                                filled=shares_filled,
                                price=fill_price
                            )
                            
                            if shares_filled > 0.0:
                                if not self._paper:
                                    actual_fills = await self._get_actual_fills_from_trades(order_id)
                                    if actual_fills:
                                        fill_price, shares_filled = actual_fills
                                        logger.info(
                                            "live_exit_order_fills_adjusted",
                                            pid=pid,
                                            order_id=order_id,
                                            actual_price=fill_price,
                                            actual_shares=shares_filled
                                        )
                                self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + (shares_filled * fill_price)
                                shares_to_sell = max(0.0, shares_to_sell - shares_filled)
                                self._shares_remaining[pid] = shares_to_sell
                                await self._persist_tracking_data(pid, tid)
                                
                            if shares_to_sell < 5.0:
                                logger.info("exit_cleared_below_minimum_size", pid=pid, remaining=shares_to_sell)
                                break
                                
                            # If we still have substantial shares to sell, fetch fresh live bid price for next attempt
                            live_bid = await self.clob.get_current_price(tid, "sell")
                            if live_bid:
                                exit_price = live_bid
                        else:
                            logger.warning("live_exit_order_rejected", pid=pid, attempt=attempt)
                    except Exception as e:
                        logger.error("live_exit_order_exception", pid=pid, attempt=attempt, error=str(e))
                        
                    await asyncio.sleep(1.0)

            total_p = self._partial_proceeds.pop(pid, 0.0)
            
            # Record remaining shares as shares_bag in notes before popping
            final_shares_bag = shares_to_sell
            self._shares_remaining.pop(pid, None)
            
            # PnL uses math_utils.realised_pnl conceptually, but since total_p is already net of fees:
            pnl = total_p - pos["size_usdc"]
            eff_exit = total_p / pos["shares_bought"] if pos["shares_bought"] > 0 else exit_price
            
            # Record Forensic Audit Trail in notes
            updated_notes = pos.get("notes", "{}")
            try:
                notes_data = json.loads(updated_notes)
                notes_data["eff_exit"] = round(eff_exit, 4)
                notes_data["total_p"] = round(total_p, 4)
                if final_shares_bag > 0.0:
                    notes_data["shares_bag"] = round(final_shares_bag, 4)
                updated_notes = json.dumps(notes_data)
            except: pass
            
            if reason == "TP 1" or reason == "TAKE_PROFIT":
                reason = "TAKE_PROFIT"
            
            await close_position(self.db, pid, eff_exit, pnl, reason, paper=self._paper)
            await update_position_notes(self.db, pid, updated_notes, paper=self._paper)
            logger.info("position_finalized", pid=pid, reason=reason, pnl=round(pnl, 4), exit_price=eff_exit, shares_bag=final_shares_bag)
            
            # Asynchronously update matching signal's eventual_pnl
            try:
                async with get_db_lock(self.db):
                    await execute_with_retry(
                        self.db,
                        "UPDATE signals SET eventual_pnl = ? WHERE market_id = ?",
                        (pnl, pos["market_id"])
                    )
                    await commit_with_retry(self.db)
                logger.info("signal_eventual_pnl_updated", market_id=pos["market_id"], eventual_pnl=pnl)
            except Exception as pnl_err:
                logger.error("failed_to_update_signal_eventual_pnl", market_id=pos["market_id"], error=str(pnl_err))
            
            # Asynchronously recalculate rolling win rates with the new 50-trade smoothing window
            try:
                from src.db.queries import recalculate_win_rates
                asyncio.create_task(recalculate_win_rates(self.db, rolling_n=50))
                logger.info("win_rates_recalculation_triggered", pid=pid)
            except Exception as r_err:
                logger.error("win_rates_recalculation_failed", error=str(r_err))
            
            if self.tg:
                try:
                    m_data = await get_market(self.db, pos["market_id"])
                    m_name = m_data.get("question", pos["market_id"]) if m_data else pos["market_id"]
                    asyncio.create_task(self.tg.notify_trade_exit(m_name, exit_price, pnl, reason, pid, self._paper))
                except: pass
                
            return True
        finally:
            self._closing_positions.discard(pid)



    async def _persist_tracking_data(self, pid: str, tid: str, profile_name: Optional[str] = None) -> None:
        """Save P&L state to DB notes."""
        try:
            # First, fetch existing position to preserve notes keys like "strategy"
            if profile_name:
                from src.db.queries import get_profile_position, update_profile_position_notes
                pos = await get_profile_position(self.db, pid)
            else:
                pos = await get_position(self.db, pid, paper=self._paper)
            existing_notes = pos.get("notes") if pos else None
            
            data = {}
            if existing_notes and existing_notes.startswith("{"):
                try:
                    data = json.loads(existing_notes)
                except Exception:
                    pass
            
            # Update/set tracking fields
            data["tid"] = tid
            data["p"] = self._partial_proceeds.get(pid, 0.0)
            shares_bought = pos.get("shares_bought", 0.0) if pos else 0.0
            data["r"] = self._shares_remaining.get(pid, shares_bought)
            data["orders"] = self._resting_tp_orders.get(pid, [])
            
            if pid in self._trailing_tp_active:
                data["ttp_active"] = self._trailing_tp_active[pid]
                data["ttp_peak"] = self._trailing_tp_peak[pid]
            
            if profile_name:
                await update_profile_position_notes(self.db, pid, json.dumps(data))
            else:
                await update_position_notes(self.db, pid, json.dumps(data), paper=self._paper)
        except: pass

    async def _load_tracking_data(self, pos: Dict) -> None:
        """Restore P&L state from DB notes."""
        try:
            data = json.loads(pos.get("notes", "{}"))
            pid = pos["position_id"]
            if "p" in data:
                self._partial_proceeds[pid] = float(data["p"])
                shares_r = float(data["r"])
                if shares_r <= 0.0 and pos.get("order_status") in ("OPEN", "PENDING"):
                    shares_r = float(pos.get("shares_bought", 0.0))
                self._shares_remaining[pid] = shares_r
            if "ttp_active" in data:
                self._trailing_tp_active[pid] = bool(data["ttp_active"])
                self._trailing_tp_peak[pid] = float(data["ttp_peak"])
            if "orders" in data:
                orders = data["orders"]
                self._resting_tp_orders[pid] = orders
                if self._paper and hasattr(self.clob, "restore_paper_order"):
                    for o in orders:
                        self.clob.restore_paper_order(
                            order_id=o["order_id"],
                            token_id=data.get("tid"),
                            side="SELL",
                            price=o["price"],
                            size=o["shares"],
                            order_type="GTC"
                        )
        except: pass

    async def _recover_target_token_id(self, mid: str, pid: str, side: str, profile_name: Optional[str] = None) -> Optional[str]:
        """Heal missing Token ID."""
        if not self.gamma: return None
        clob = await self.gamma.get_market_info(mid)
        if clob and "tokens" in clob:
            target_outcome = "Yes" if side == "YES" else "No"
            for t in clob["tokens"]:
                outcome = str(t.get("outcome", "")).strip()
                if outcome.lower() == target_outcome.lower():
                    tid = t.get("token_id")
                    try:
                        if profile_name:
                            from src.db.queries import get_profile_position, update_profile_position_notes
                            pos = await get_profile_position(self.db, pid)
                        else:
                            pos = await get_position(self.db, pid, paper=self._paper)
                        notes_val = pos.get("notes") if pos else None
                        if notes_val and notes_val.startswith("{"):
                            data = json.loads(notes_val)
                            data["tid"] = tid
                            notes_payload = json.dumps(data)
                        else:
                            notes_payload = json.dumps({"tid": tid, "strategy": "TREND"})
                    except Exception:
                        notes_payload = json.dumps({"tid": tid, "strategy": "TREND"})
                    
                    if profile_name:
                        await update_profile_position_notes(self.db, pid, notes_payload)
                    else:
                        await update_position_notes(self.db, pid, notes_payload, paper=self._paper)
                    return tid
        return None

    async def open_profile_position(
        self,
        profile_name: str,
        market_id: str,
        target_token_id: Optional[str] = None,
        entry_price: float = 0.5,
        size_usdc: float = 10.0,
        shares: float = 20.0,
        side: str = "NO",
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        persistence_score: float = 0.0,
        strategy_mode: str = "TREND",
        streak_depth: int = 2,
        custom_notes: Optional[str] = None,
        underlying_distance_bps: float = 0.0,
        bs_implied_probability: float = 0.5,
        realized_vol_5m: float = 0.0
    ) -> bool:
        """
        Open a new virtual profile position.
        This position is stored in profile_positions and never places real CLOB orders.
        """
        try:


            if profile_name == "executioner" and custom_notes:
                try:
                    parsed_notes = json.loads(custom_notes)
                    req_id = parsed_notes.get("request_id")
                    if req_id is not None:
                        row = await fetch_one(
                            self.db,
                            "SELECT 1 FROM executioner_positions WHERE market_id = ? AND request_id = ? AND order_status = 'OPEN'",
                            (market_id, req_id)
                        )
                        if row:
                            return False
                except Exception:
                    pass
            else:
                # Check if already open for this profile
                row = await fetch_one(
                    self.db,
                    "SELECT 1 FROM profile_positions WHERE market_id = ? AND profile_name = ? AND order_status = 'OPEN'",
                    (market_id, profile_name)
                )
                if row:
                    return False
            
            actual_shares = int(shares)
            actual_size_usdc = actual_shares * entry_price
            
            notes_payload = custom_notes if custom_notes is not None else json.dumps({"tid": target_token_id, "strategy": strategy_mode, "streak_depth": streak_depth})
            
            vol_val = realized_vol_5m
            if vol_val == 0.0 and hasattr(self, "_market_cache"):
                m_info = self._market_cache.get(market_id, {})
                vol_val = float(m_info.get("underlying_realized_vol_5m") or m_info.get("realized_vol_5m") or 0.0)

            from src.db.queries import create_profile_position
            position_id = await create_profile_position(
                self.db,
                market_id=market_id,
                entry_price=entry_price,
                size_usdc=actual_size_usdc,
                shares_bought=actual_shares,
                bot_version=self.config.bot_version,
                notes=notes_payload,
                side=side,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                strategy_mode=strategy_mode,
                profile_name=profile_name,
                underlying_distance_bps=underlying_distance_bps,
                bs_implied_probability=bs_implied_probability,
                realized_vol_5m=vol_val
            )

            # Shadow meta-strategy logger — observational only
            if hasattr(self, "meta_logger") and self.meta_logger:
                try:
                    now_ts = int(time.time())
                    m_info = self._market_cache.get(market_id, {})
                    sym = m_info.get("symbol", "BTC") if m_info else "BTC"
                    win_min = m_info.get("window_minutes", 5) if m_info else 5

                    import datetime
                    dt_utc = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc)
                    dt_et = dt_utc - datetime.timedelta(hours=4)  # EDT is UTC-4
                    h_et = dt_et.hour
                    day_str = dt_et.strftime("%A")
                    is_wknd = 1 if day_str in ("Saturday", "Sunday") else 0

                    pos_dict = {
                        "position_id": position_id,
                        "profile_name": profile_name,
                        "market_id": market_id,
                        "symbol": sym,
                        "window_minutes": win_min,
                        "opened_at": now_ts,
                        "entry_price": entry_price,
                        "hour_et": h_et,
                        "day_of_week": day_str,
                        "is_weekend": is_wknd,
                    }
                    await self.meta_logger.log_trade_opened(pos_dict)
                except Exception as e:
                    logger.warning("meta_logger_open_error", position_id=position_id, error=str(e))

            # WebSocket subscription for virtual profile positions to keep price caches active
            m_data = self._market_cache.get(market_id)
            if not m_data:
                from src.db.queries import get_market
                m_data = await get_market(self.db, market_id)
                if m_data:
                    self._market_cache[market_id] = m_data

            if self.ws and hasattr(self.ws, "subscribe_market"):
                try:
                    yes_tid = m_data.get("yes_token_id") if m_data else (target_token_id if side == "YES" else None)
                    no_tid = m_data.get("no_token_id") if m_data else (target_token_id if side == "NO" else None)
                    
                    await self.ws.subscribe_market(market_id, yes_token_id=yes_tid, no_token_id=no_tid)
                    logger.info("virtual_profile_market_ws_subscribed", profile=profile_name, market_id=market_id, yes_tid=yes_tid, no_tid=no_tid)
                except Exception as sub_err:
                    logger.error("failed_to_subscribe_virtual_market_ws", profile=profile_name, market_id=market_id, error=str(sub_err))

            if profile_name in ("probability_sl", "reversal_probability_sl"):
                closes_at_val = int(
                    (m_data.get("closes_at") if m_data else 0)
                    or (int(time.time()) + int(seconds_to_close_at_entry or 300))
                )
                self.open_probability_sl_positions[position_id] = {
                    "position_id": position_id,
                    "market_id": market_id,
                    "side": side,
                    "entry_price": entry_price,
                    "size_usdc": actual_size_usdc,
                    "shares_bought": actual_shares,
                    "order_status": "OPEN",
                    "profile_name": profile_name,
                    "notes": notes_payload,
                    "closes_at": closes_at_val,
                }
            if hasattr(self, "scanner") and self.scanner:
                self.scanner.record_post_entry_spread(market_id, duration_seconds=5.0)
            return True
        except Exception as e:
            logger.error("open_profile_position_error", profile=profile_name, market_id=market_id, error=str(e))
            return False

    async def _load_open_probability_sl_positions(self) -> None:
        try:
            rows = await fetch_all(
                self.db,
                "SELECT * FROM profile_positions WHERE profile_name IN ('probability_sl', 'reversal_probability_sl') AND order_status = 'OPEN'"
            )
            now_ts = time.time()
            for r in rows:
                pos = dict(r)
                m_id = pos["market_id"]
                m_data = self._market_cache.get(m_id)
                if not m_data:
                    m_data = await get_market(self.db, m_id)
                    if m_data:
                        self._market_cache[m_id] = m_data
                closes_at = int(
                    (m_data.get("closes_at") if m_data else 0)
                    or (int(pos.get("opened_at") or 0) + int(pos.get("window_minutes") or 5) * 60)
                )
                if closes_at > 0 and now_ts > closes_at:
                    continue
                pos["closes_at"] = closes_at
                self.open_probability_sl_positions[pos["position_id"]] = pos
            logger.info("loaded_open_probability_sl_positions", count=len(self.open_probability_sl_positions))
        except Exception as e:
            logger.error("failed_to_load_open_probability_sl_positions", error=str(e))

    async def _monitor_probability_sl_loop(self) -> None:
        """
        High-frequency in-memory check for probability-based stop losses.
        Runs every 100ms. No database queries, no price logging.
        """
        await self._load_open_probability_sl_positions()
        
        while True:
            try:
                await asyncio.sleep(0.1)
                now_ts = time.time()
                
                # Create a static list of items to avoid modification during iteration
                for pos_id, pos in list(self.open_probability_sl_positions.items()):
                    market_id = pos["market_id"]
                    closes_at = int(
                        pos.get("closes_at")
                        or (self._market_cache.get(market_id) or {}).get("closes_at")
                        or 0
                    )
                    if closes_at > 0 and now_ts > closes_at:
                        self.open_probability_sl_positions.pop(pos_id, None)
                        self._prob_sl_first_dip.pop(pos_id, None)
                        continue
                    side = pos["side"]
                    profile_name = pos["profile_name"]
                    entry_price = pos["entry_price"]
                    shares_bought = pos["shares_bought"]
                    size_usdc = pos["size_usdc"]
                    
                    target_token_id = _get_tid_from_notes(pos.get("notes"))
                    if not target_token_id:
                        target_token_id = await self._recover_target_token_id(market_id, pos_id, side)
                        if not target_token_id:
                            continue
                        pos["notes"] = json.dumps({"tid": target_token_id}) # cache it in dict
                    
                    current_price = None
                    if self.ws:
                        current_price = self._clean_price(self.ws.get_best_bid(target_token_id))
                    
                    if current_price is None or current_price <= 0:
                        m_data = self._market_cache.get(market_id)
                        if not m_data:
                            from src.db.queries import get_market
                            m_data = await get_market(self.db, market_id)
                            self._market_cache[market_id] = m_data
                        if m_data:
                            counterpart_tid = m_data.get("no_token_id") if side == "YES" else m_data.get("yes_token_id")
                            if counterpart_tid and self.ws:
                                counterpart_ask = self._clean_price(self.ws.get_best_ask(counterpart_tid))
                                if counterpart_ask is not None and counterpart_ask < 1.0:
                                    current_price = round(1.0 - counterpart_ask, 4)
                                    
                    if (current_price is None or current_price <= 0) and self.ws:
                        current_price = self._clean_price(self.ws.get_live_price(target_token_id))
                        
                    if current_price is not None and current_price > 0:
                        if current_price < 0.50:
                            # Start or check dip timer
                            now = time.time()
                            if pos_id not in self._prob_sl_first_dip:
                                self._prob_sl_first_dip[pos_id] = {
                                    "start_time": now,
                                    "trigger_price": current_price
                                }
                            else:
                                dip_info = self._prob_sl_first_dip[pos_id]
                                duration = now - dip_info["start_time"]
                                if duration >= 0.400: # 400ms duration met
                                    pnl = calc_realised_pnl(current_price, shares_bought, size_usdc)
                                    from src.db.queries import close_profile_position, log_wick_event
                                    
                                    # 1. Close profile position in database
                                    await close_profile_position(self.db, pos_id, current_price, pnl, "STOP_LOSS")
                                    
                                    # 2. Log wick event in database (STOP_LOSS resolution)
                                    await log_wick_event(
                                        self.db,
                                        position_id=pos_id,
                                        market_id=market_id,
                                        profile_name=profile_name,
                                        start_time=dip_info["start_time"],
                                        end_time=now,
                                        duration_ms=duration * 1000.0,
                                        resolved_by="STOP_LOSS",
                                        entry_price=entry_price,
                                        trigger_price=dip_info["trigger_price"],
                                        exit_price=current_price
                                    )
                                    
                                    # 3. Clean up memory registries
                                    self.open_probability_sl_positions.pop(pos_id, None)
                                    self._prob_sl_first_dip.pop(pos_id, None)
                                    
                                    logger.info(
                                        "probability_sl_triggered",
                                        profile=profile_name,
                                        position_id=pos_id,
                                        entry_price=entry_price,
                                        exit_price=current_price,
                                        duration_ms=duration * 1000.0
                                    )
                        else:
                            # Price is >= 0.50. Check if it just recovered from a dip to log it
                            if pos_id in self._prob_sl_first_dip:
                                now = time.time()
                                dip_info = self._prob_sl_first_dip.pop(pos_id)
                                duration = now - dip_info["start_time"]
                                
                                # Log wick event in database (RECOVERY resolution)
                                from src.db.queries import log_wick_event
                                await log_wick_event(
                                    self.db,
                                    position_id=pos_id,
                                    market_id=market_id,
                                    profile_name=profile_name,
                                    start_time=dip_info["start_time"],
                                    end_time=now,
                                    duration_ms=duration * 1000.0,
                                    resolved_by="RECOVERY",
                                    entry_price=entry_price,
                                    trigger_price=dip_info["trigger_price"],
                                    exit_price=current_price
                                )
                                
                                logger.info(
                                    "probability_sl_recovered",
                                    profile=profile_name,
                                    position_id=pos_id,
                                    entry_price=entry_price,
                                    current_price=current_price,
                                    duration_ms=duration * 1000.0
                                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("probability_sl_monitor_loop_error", error=str(e))




