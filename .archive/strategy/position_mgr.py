"""
Position Manager — Open/Monitor/Close Positions for Polymarket Reverse Bot.

Handles:
- Opening: PENDING → place FOK → confirm fill → OPEN or FAILED
- Monitoring: TP, SL, resolution check every cycle
- Closing: FOK sell → retry once at -1¢ → CLOSE_FAILED on failure

MISTAKE 8 GUARD: asyncio.Lock() per market_id prevents race conditions.
DB partial unique index is the secondary guard.

MISTAKE 7 GUARD: Always confirms fill via order status endpoint.
Never assumes fill from HTTP 200 placement response.
"""

import asyncio
import time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.config import BotConfig
from src.api.clob_client import ClobClient
from src.db.queries import (
    create_position,
    update_position_status,
    close_position,
    get_open_positions,
    get_position,
    has_open_position_for_market,
    log_event,
)
from src.logs.log_config import get_logger

logger = get_logger("position_mgr")

# Per-market locks to prevent race conditions
_market_locks: Dict[str, asyncio.Lock] = {}


async def get_market_lock(market_id: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific market_id."""
    if market_id not in _market_locks:
        _market_locks[market_id] = asyncio.Lock()
    return _market_locks[market_id]


class PositionManager:
    """
    Manages the full lifecycle of trading positions:
    Open → Monitor → Close
    """

    def __init__(
        self,
        config: BotConfig,
        clob_client: ClobClient,
        db,
        ws_client=None,
    ):
        self.config = config
        self.clob = clob_client
        self.db = db
        self.ws = ws_client
        self._paper = config.paper_trade

        mode = "PAPER" if self._paper else "LIVE"
        logger.info(
            "position_mgr_initialized",
            mode=mode,
            tp_multiplier=config.take_profit_multiplier,
            sl_multiplier=config.stop_loss_multiplier,
            message=f"Position manager ready in {mode} mode",
        )

    # -----------------------------------------------------------------
    # Opening Positions
    # -----------------------------------------------------------------

    async def open_position(
        self,
        market_id: str,
        no_token_id: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
    ) -> bool:
        """
        Open a new position with full safety guards:
        1. Acquire market lock (race condition protection)
        2. Write PENDING to DB
        3. Place FOK order
        4. Confirm fill (never assume from 200)
        5. Update to OPEN or FAILED

        Returns True if position successfully opened.
        """
        lock = await get_market_lock(market_id)

        async with lock:
            already_open = await has_open_position_for_market(
                self.db,
                market_id,
                paper=self._paper,
            )
            if already_open:
                logger.warning(
                    "position_already_open",
                    market_id=market_id,
                    message="Position already OPEN for market — skipping duplicate open request",
                )
                return False

            # Enforce integer shares for both DB and Exchange (MISTAKE 3 FIX)
            actual_shares = int(shares)
            
            position_id = await create_position(
                self.db,
                market_id=market_id,
                entry_price=entry_price,
                size_usdc=size_usdc,
                shares_bought=actual_shares,
                bot_version=self.config.bot_version,
                paper=self._paper,
                notes=no_token_id,
            )

            logger.info(
                "position_pending",
                position_id=position_id,
                market_id=market_id,
                entry_price=entry_price,
                size_usdc=size_usdc,
                shares=actual_shares,
                message=f"Position created as PENDING — placing FOK order",
            )

            # Step 2: Place FOK order
            try:
                order_result = await self.clob.place_order(
                    token_id=no_token_id,
                    side="BUY",
                    price=entry_price,
                    size=actual_shares,
                    order_type="FOK",  # Enforced by assertion in clob_client
                )
            except AssertionError as e:
                # This should never happen — but if it does, fail safely
                logger.critical(
                    "order_type_violation",
                    position_id=position_id,
                    error=str(e),
                    message="ORDER TYPE ASSERTION FAILED — this is a code bug",
                )
                await update_position_status(
                    self.db, position_id, "FAILED", paper=self._paper
                )
                return False

            if not order_result:
                # Order rejected by API
                await update_position_status(
                    self.db, position_id, "FAILED", paper=self._paper
                )
                logger.error(
                    "order_placement_failed",
                    position_id=position_id,
                    market_id=market_id,
                    message="Order placement failed — position marked FAILED",
                )
                return False

            order_id = order_result.get("orderID") or order_result.get("id", "unknown")

            # Step 3: Confirm fill (MISTAKE 7 GUARD)
            # HTTP 200 = accepted, NOT filled. Must poll status.
            fill_status = await self.clob.wait_for_fill(
                order_id,
                timeout_seconds=15.0,
                poll_interval=1.0,
            )

            if fill_status == "MATCHED":
                await update_position_status(
                    self.db, position_id, "OPEN",
                    order_id=order_id, paper=self._paper,
                )

                logger.info(
                    "position_opened",
                    position_id=position_id,
                    market_id=market_id,
                    order_id=order_id,
                    entry_price=entry_price,
                    shares=round(shares, 2),
                    size_usdc=size_usdc,
                    message=f"POSITION OPENED — {shares:.0f} No shares @ {entry_price}",
                )

                await log_event(
                    self.db,
                    event_type="FILL",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    position_id=position_id,
                    payload={
                        "order_id": order_id,
                        "entry_price": entry_price,
                        "shares": shares,
                        "size_usdc": size_usdc,
                    },
                )
                return True

            elif fill_status == "PARTIAL":
                # PARTIAL_FILL: Log alert, cancel remainder, pause market
                logger.error(
                    "partial_fill",
                    position_id=position_id,
                    market_id=market_id,
                    order_id=order_id,
                    message="PARTIAL FILL — cancelling remainder, pausing market",
                )
                await self.clob.cancel_order(order_id)
                await update_position_status(
                    self.db, position_id, "PARTIAL",
                    order_id=order_id, paper=self._paper,
                )
                return False

            else:
                # Order was not filled (cancelled, expired, or timeout)
                await update_position_status(
                    self.db, position_id, "FAILED",
                    order_id=order_id, paper=self._paper,
                )
                logger.warning(
                    "order_not_filled",
                    position_id=position_id,
                    market_id=market_id,
                    order_id=order_id,
                    fill_status=fill_status,
                    message=f"Order not filled — status: {fill_status}",
                )
                return False

    # -----------------------------------------------------------------
    # Position Monitoring
    # -----------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """
        Check all OPEN positions for TP/SL/resolution.
        Called every poll cycle.
        """
        positions = await get_open_positions(self.db, paper=self._paper)

        if not positions:
            return

        for pos in positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                logger.error(
                    "position_monitor_error",
                    position_id=pos["position_id"],
                    market_id=pos["market_id"],
                    error=str(e),
                    message=f"Error monitoring position: {e}",
                )

    def _get_overrides(self) -> Dict[str, Any]:
        """Load TP/SL overrides from JSON if it exists."""
        overrides_path = Path("dashboard/position_overrides.json")
        if overrides_path.exists():
            try:
                return json.loads(overrides_path.read_text())
            except Exception:
                pass
        return {}

    async def _check_position(self, pos: Dict[str, Any]) -> None:
        """
        Check a single position for exit conditions:
        1. Take profit: current_value >= size * TP_MULTIPLIER
        2. Stop loss: current_value <= size * SL_MULTIPLIER
        3. Resolution: market has closed/resolved
        """
        position_id = pos["position_id"]
        market_id = pos["market_id"]
        entry_price = pos["entry_price"]
        size_usdc = pos["size_usdc"]
        shares = pos["shares_bought"]
        order_id = pos.get("order_id", "")

        # Get current No price
        token_id = pos.get("notes") or market_id
        current_no_price = None

        # Try WebSocket cache first (most efficient)
        if self.ws:
            # First try the token_id (real No side)
            if token_id in self.ws.price_cache:
                current_no_price = self.ws.price_cache[token_id]
            # Fallback to market_id (complement of Yes) if token_id is missing
            elif market_id in self.ws.price_cache:
                current_no_price = 1.0 - self.ws.price_cache[market_id]

        # Fallback to REST API if WS misses the price
        if current_no_price is None and not self._paper:
            logger.debug("ws_price_missing_trying_rest", market_id=market_id)
            # Use token_id (real No price)
            current_no_price = await self.clob.get_current_price(token_id)

        if current_no_price is None:
            # Final fallback to entry price if in paper mode, otherwise skip
            if self._paper:
                current_no_price = entry_price
            else:
                logger.warning(
                    "price_fetch_failed",
                    position_id=position_id,
                    market_id=market_id,
                    message="Could not fetch price via WS or REST — skipping check",
                )
                return

        # Calculate current value
        current_value = current_no_price * shares

        # Unrealised P&L
        unrealised_pnl = (current_value * 0.98) - size_usdc

        logger.debug(
            "position_monitored",
            position_id=position_id,
            market_id=market_id,
            entry_price=entry_price,
            current_no_price=current_no_price,
            shares=round(shares, 2),
            current_value=round(current_value, 2),
            unrealised_pnl=round(unrealised_pnl, 2),
            message=f"Monitoring: value={current_value:.2f} P&L={unrealised_pnl:.2f}",
        )

        overrides = self._get_overrides().get(position_id, {})
        tp_mult = overrides.get("tp_multiplier", self.config.take_profit_multiplier)
        sl_mult = overrides.get("sl_multiplier", self.config.stop_loss_multiplier)
        hold_res = getattr(self.config, "hold_to_resolution", False)

        # Resolution check (done early to calculate time_to_close)
        from src.db.queries import get_market
        market = await get_market(self.db, market_id)
        closes_at = market.get("closes_at", 0) if market else 0
        now = int(time.time())
        seconds_to_close = closes_at - now if closes_at else 9999
        
        # Take Profit check
        if not hold_res:
            tp_target = size_usdc * tp_mult
            if current_value >= tp_target:
                logger.info(
                    "take_profit_triggered",
                    position_id=position_id,
                    market_id=market_id,
                    current_value=round(current_value, 2),
                    tp_target=round(tp_target, 2),
                    message=f"TAKE PROFIT — value {current_value:.2f} >= target {tp_target:.2f}",
                )
                await self._close_position(pos, current_no_price, "TAKE_PROFIT")
                return

            # Stop Loss check with Whipsaw Buffer
            sl_target = size_usdc * sl_mult
            if current_value <= sl_target:
                # Whipsaw Buffer: Ignore Stop-Loss if within 45 seconds of market close
                if seconds_to_close > 0 and seconds_to_close <= 45:
                    logger.info(
                        "stop_loss_ignored_whipsaw",
                        position_id=position_id,
                        market_id=market_id,
                        seconds_to_close=seconds_to_close,
                        message=f"STOP LOSS triggered but ignored due to Resolution Whipsaw buffer ({seconds_to_close}s left)",
                    )
                else:
                    logger.info(
                        "stop_loss_triggered",
                        position_id=position_id,
                        market_id=market_id,
                        current_value=round(current_value, 2),
                        sl_target=round(sl_target, 2),
                        message=f"STOP LOSS — value {current_value:.2f} <= target {sl_target:.2f}",
                    )
                    await self._close_position(pos, current_no_price, "STOP_LOSS")
                    return

        if market and market.get("closes_at"):
            if now > market["closes_at"]:
                if self._paper:
                    # Fetch outcome directly from Gamma to ensure accurate 0.0 or 1.0 Paper PnL
                    try:
                        import aiohttp
                        import json
                        async with aiohttp.ClientSession() as session:
                            async with session.get(f"https://gamma-api.polymarket.com/markets/{market_id}") as resp:
                                g_data = await resp.json()
                                if g_data.get("closed"):
                                    raw_prices = g_data.get("outcomePrices", "[]")
                                    if isinstance(raw_prices, str):
                                        prices = json.loads(raw_prices)
                                    else:
                                        prices = raw_prices
                                        
                                    if len(prices) == 2:
                                        current_no_price = float(prices[1])
                                        logger.info(
                                            "resolution_close_paper",
                                            position_id=position_id,
                                            market_id=market_id,
                                            no_price=current_no_price,
                                            message="Market resolved — closing paper position with oracle price",
                                        )
                                        await self._close_position(pos, current_no_price, "MARKET_OUTCOME")
                                        return
                    except Exception as e:
                        logger.error("paper_resolution_error", error=str(e))
                    return # Wait for next cycle to check if closed
                else:
                    logger.info(
                        "resolution_close",
                        position_id=position_id,
                        market_id=market_id,
                        message="Market has closed/resolved — closing position",
                    )
                    await self._close_position(pos, current_no_price, "MARKET_OUTCOME")

    async def manual_close_position(self, position_id: str) -> None:
        """Triggered from dashboard to close a specific position immediately."""
        pos = await get_position(self.db, position_id, paper=self._paper)
        if not pos:
            raise ValueError(f"Position {position_id} not found")
            
        if pos["order_status"] != "OPEN":
            raise ValueError(f"Position {position_id} is {pos['order_status']}, cannot close")

        # Get current price
        market_id = pos["market_id"]
        token_id = pos.get("notes") or market_id
        current_no_price = None

        if self.ws and market_id in self.ws.price_cache:
            current_no_price = 1.0 - self.ws.price_cache[market_id]

        if current_no_price is None and not self._paper:
            current_no_price = await self.clob.get_current_price(token_id)
            
        if current_no_price is None:
            if self._paper:
                current_no_price = pos["entry_price"]
            else:
                raise ValueError("Could not fetch current price to close position")

        logger.warning(
            "manual_close_triggered", 
            position_id=position_id, 
            market_id=market_id,
            message=f"MANUAL CLOSE triggered for {position_id}"
        )
        await self._close_position(pos, current_no_price, "MANUAL_CLOSE")

    # -----------------------------------------------------------------
    # Closing Positions
    # -----------------------------------------------------------------

    async def _close_position(
        self,
        pos: Dict[str, Any],
        exit_price: float,
        reason: str,
    ) -> bool:
        """
        Close a position:
        - Paper mode: directly record the close with correct PnL (no fake CLOB)
        - Live mode: Place FOK sell → retry once → mark CLOSE_FAILED
        """
        position_id = pos["position_id"]
        market_id = pos["market_id"]
        shares = pos["shares_bought"]
        size_usdc = pos["size_usdc"]
        entry_price = pos.get("entry_price", exit_price)
        token_id = pos.get("notes") or market_id

        # ── Paper mode: skip the CLOB entirely, record PnL directly ──────────
        if self._paper:
            # Calculate real PnL without artificial zero-rounding
            realised_pnl = (exit_price * shares * 0.98) - size_usdc

            await close_position(
                self.db, position_id, exit_price, realised_pnl, reason,
                paper=True,
            )
            logger.info(
                "paper_position_closed",
                position_id=position_id,
                market_id=market_id,
                exit_price=exit_price,
                entry_price=entry_price,
                realised_pnl=round(realised_pnl, 2),
                close_reason=reason,
                message=f"PAPER CLOSE — P&L: ${realised_pnl:.2f} ({reason})",
            )
            return True

        # ── Live mode: attempt real FOK sell ─────────────────────────────────
        # Attempt 1: sell at exit_price
        sell_result = await self.clob.place_order(
            token_id=token_id,
            side="SELL",
            price=exit_price,
            size=shares,
            order_type="FOK",
        )

        if sell_result:
            sell_id = sell_result.get("orderID", "")
            fill_status = await self.clob.wait_for_fill(sell_id, timeout_seconds=10.0)

            if fill_status == "MATCHED":
                realised_pnl = (exit_price * shares * 0.98) - size_usdc
                await close_position(
                    self.db, position_id, exit_price, realised_pnl, reason,
                    paper=self._paper,
                )

                logger.info(
                    "position_closed",
                    position_id=position_id,
                    market_id=market_id,
                    exit_price=exit_price,
                    realised_pnl=round(realised_pnl, 2),
                    close_reason=reason,
                    message=f"POSITION CLOSED — P&L: ${realised_pnl:.2f} ({reason})",
                )

                await log_event(
                    self.db,
                    event_type="POSITION_CLOSED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    position_id=position_id,
                    payload={
                        "exit_price": exit_price,
                        "realised_pnl": realised_pnl,
                        "close_reason": reason,
                    },
                )
                return True

        # Attempt 2: retry at -1 cent after 5 seconds
        logger.warning(
            "close_retry",
            position_id=position_id,
            market_id=market_id,
            message="First close attempt failed — retrying at -1¢ in 5s",
        )

        await asyncio.sleep(5.0)
        retry_price = max(0.01, exit_price - 0.01)

        retry_result = await self.clob.place_order(
            token_id=token_id,
            side="SELL",
            price=retry_price,
            size=shares,
            order_type="FOK",
        )

        if retry_result:
            retry_id = retry_result.get("orderID", "")
            fill_status = await self.clob.wait_for_fill(retry_id, timeout_seconds=10.0)

            if fill_status == "MATCHED":
                realised_pnl = (retry_price * shares * 0.98) - size_usdc
                await close_position(
                    self.db, position_id, retry_price, realised_pnl, reason,
                    paper=self._paper,
                )

                logger.info(
                    "position_closed_retry",
                    position_id=position_id,
                    market_id=market_id,
                    exit_price=retry_price,
                    realised_pnl=round(realised_pnl, 2),
                    close_reason=reason,
                    message=f"POSITION CLOSED (retry) — P&L: ${realised_pnl:.2f}",
                )
                return True

        # Both attempts failed
        await update_position_status(
            self.db, position_id, "CLOSE_FAILED", paper=self._paper,
        )

        logger.critical(
            "close_failed",
            position_id=position_id,
            market_id=market_id,
            close_reason=reason,
            message="CLOSE FAILED — both attempts failed. OPERATOR ALERT REQUIRED.",
        )

        await log_event(
            self.db,
            event_type="CLOSE_FAILED",
            bot_version=self.config.bot_version,
            market_id=market_id,
            position_id=position_id,
            payload={"reason": reason, "attempts": 2},
        )

        return False

    async def close_all(self, reason: str = "KILL_SWITCH") -> int:
        """
        Close all open positions. Used by kill switch.
        Returns count of successfully closed positions.
        """
        positions = await get_open_positions(self.db, paper=self._paper)
        closed = 0

        for pos in positions:
            current_price = await self.clob.get_current_price(
                pos.get("order_id", pos["market_id"])
            )
            if current_price is None:
                current_price = pos["entry_price"]  # fallback

            success = await self._close_position(pos, current_price, reason)
            if success:
                closed += 1

        logger.info(
            "close_all_complete",
            total=len(positions),
            closed=closed,
            failed=len(positions) - closed,
            reason=reason,
            message=f"Close all: {closed}/{len(positions)} positions closed",
        )

        return closed
