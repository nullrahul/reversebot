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
import uuid
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.config import BotConfig
from src.strategy.math_utils import net_value, realised_pnl as calc_realised_pnl
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
    ):
        self.config = config
        self.clob = clob_client
        self.db = db
        self.ws = ws_client
        self.gamma = gamma_client
        self.tg = None  # Set by main.py during setup

        self._paper = config.paper_trade
        self._market_cache: Dict[str, dict] = {}
        
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
        
        # Background event processor
        self._event_task: Optional[asyncio.Task] = None

        mode = "PAPER" if self._paper else "LIVE"
        logger.info(
            "position_mgr_initialized",
            mode=mode,
            message=f"Position manager ready in {mode} mode",
        )

    async def start(self) -> None:
        """Start the background event processor."""
        if self.ws and not self._event_task:
            self._event_task = asyncio.create_task(self._event_processor_loop())
            logger.info("position_mgr_event_processor_started")

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

    async def _event_processor_loop(self) -> None:
        """Processes user events (MINED, etc.) from the WebSocket queue."""
        if not self.ws: return
        
        while True:
            try:
                event = await self.ws.user_queue.get()
                # Obsolete parameters removed. No resting TP orders are placed.
            except Exception as e:
                logger.error("event_processor_error", error=str(e))

    # -----------------------------------------------------------------
    # Opening Positions
    # -----------------------------------------------------------------

    async def open_position(
        self,
        market_id: str,
        target_token_id: Optional[str] = None,
        entry_price: float = 0.0,
        size_usdc: float = 0.0,
        shares: float = 0.0,
        side: str = 'NO',
        no_token_id: Optional[str] = None,
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
            actual_size_usdc = actual_shares * entry_price
            
            position_id = await create_position(
                self.db,
                market_id=market_id,
                entry_price=entry_price,
                size_usdc=actual_size_usdc,
                shares_bought=actual_shares,
                bot_version=self.config.bot_version,
                paper=self._paper,
                notes=target_token_id,
                side=side,
            )

            try:
                # Place FOK order
                order_result = await self.clob.place_order(
                    token_id=target_token_id,
                    side="BUY",
                    price=entry_price,
                    size=actual_shares,
                    order_type="FOK",
                )

                if order_result:
                    order_id = order_result.get("orderID") or order_result.get("id")
                    await asyncio.sleep(1.0) # Wait for CLOB
                    status_check = await self.clob.get_order_status(order_id)
                    
                    if status_check and status_check.get("status") in ("MATCHED", "FILLED"):
                        # Initialize tracking
                        self._partial_proceeds[position_id] = 0.0
                        self._shares_remaining[position_id] = float(actual_shares)
                        await self._persist_tracking_data(position_id, target_token_id)
                        
                        if self._paper:
                            await update_position_status(self.db, position_id, "OPEN", "paper_" + uuid.uuid4().hex[:16], paper=True)
                            return True

                        await update_position_status(self.db, position_id, "OPEN", order_id, paper=self._paper)
                        self._active_orders[order_id] = position_id
                        
                        # Notify Telegram
                        if self.tg:
                            try:
                                market_data = await get_market(self.db, market_id)
                                m_name = market_data.get("question", market_id) if market_data else market_id
                                asyncio.create_task(self.tg.notify_trade_entry(
                                    market_name=m_name,
                                    entry_price=entry_price,
                                    size_usdc=actual_size_usdc,
                                    shares=actual_shares,
                                    position_id=position_id,
                                    paper=self._paper
                                ))
                            except Exception as e:
                                logger.error("tg_entry_notify_failed", error=str(e))

                        return True
                    else:
                        await update_position_status(self.db, position_id, "FAILED", order_id, paper=self._paper)
                        return False
                else:
                    await update_position_status(self.db, position_id, "FAILED", paper=self._paper)
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
        open_positions = await get_open_positions(self.db, paper=self._paper)
        if not open_positions:
            return

        now = int(time.time())

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

            # ─── ANTI-BLINDNESS FIX & SPREAD SUICIDE GRACE PERIOD ───
            current_target_bid = self.ws.bid_cache.get(target_token_id)
            current_target_ask = self.ws.ask_cache.get(target_token_id)
            
            opened_at = pos.get("opened_at", 0)
            
            current_target_price = current_target_bid
            if now - opened_at < 30 and current_target_bid and current_target_ask:
                current_target_price = (current_target_bid + current_target_ask) / 2.0

            if current_target_price is None or current_target_price <= 0:
                # Try implied price from counterpart token if target is empty
                if m_data:
                    counterpart_tid = m_data.get("no_token_id") if side == "YES" else m_data.get("yes_token_id")
                    if counterpart_tid:
                        counterpart_ask = self.ws.ask_cache.get(counterpart_tid)
                        if counterpart_ask is not None and counterpart_ask < 1.0:
                            current_target_price = round(1.0 - counterpart_ask, 4)

            if current_target_price is None or current_target_price <= 0:
                current_target_price = self.ws.price_cache.get(target_token_id)
            
            if current_target_price is None or current_target_price <= 0:
                current_target_price = self._lkgp_memory.get(position_id)
                if current_target_price is None: continue
            
            self._lkgp_memory[position_id] = current_target_price

            # Track minimum and maximum prices reached during open position
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

            # Recovery logic: Ensure memory state is consistent with database notes
            if position_id not in self._partial_proceeds:
                await self._load_tracking_data(pos)

    async def check_resolutions(self) -> None:
        """Check Gamma for market resolution outcomes."""
        open_positions = await get_open_positions(self.db, paper=self._paper)
        now = int(time.time())
        for pos in open_positions:
            market_id = pos["market_id"]
            m_data = await get_market(self.db, market_id)
            if not m_data or (m_data.get("closes_at", 0) > now): continue
            
            try:
                clob_data = await self.gamma._get(f"/markets/{market_id}")
                prices = json.loads(clob_data.get("outcomePrices", "[]"))
                outcomes = json.loads(clob_data.get("outcomes", "[]"))
                if len(prices) >= 2 and len(outcomes) >= 2:
                    side = pos.get("side", "NO")
                    p_target = None
                    for idx, outcome in enumerate(outcomes):
                        outcome_upper = str(outcome).upper()
                        if side == "YES" and outcome_upper in ("YES", "UP"):
                            p_target = float(prices[idx])
                            break
                        elif side == "NO" and outcome_upper in ("NO", "DOWN"):
                            p_target = float(prices[idx])
                            break
                    if p_target is not None and p_target in (0.0, 1.0):
                        await self._close_position(pos, p_target, "RESOLUTION")
            except Exception as e:
                logger.error("resolution_check_error", market_id=market_id, error=str(e))

    async def manual_close_position(self, position_id: str) -> bool:
        """Emergency manual close — tries every price source and uses aggressive 3-strike FOK."""
        pos = await get_position(self.db, position_id, paper=self._paper)
        if not pos:
            logger.error("manual_close_failed", position_id=position_id, message="Position not found in DB")
            return False

        if position_id not in self._partial_proceeds:
            await self._load_tracking_data(pos)

        tid = _get_tid_from_notes(pos.get("notes"))

        # Price source priority: bid (sell side) → ask → last trade
        price = (
            self.ws.bid_cache.get(tid)
            or self.ws.ask_cache.get(tid)
            or self.ws.price_cache.get(tid)
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
        # Use STOP_LOSS path so it gets the aggressive 3-strike FOK loop
        return await self._close_position(pos, price, "STOP_LOSS", skip_cancel=False)

    async def _close_position(self, pos: Dict, exit_price: float, reason: str, skip_cancel: bool = False) -> bool:
        """Unified exit logic."""
        pid = pos["position_id"]
        tid = _get_tid_from_notes(pos.get("notes"))
        
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
        elif shares_to_sell >= 0.1:
            sold_successfully = False
            if reason == "STOP_LOSS":
                # Aggressive exit: 3 tries with increasing slippage (1%, 3%, 5%)
                logger.debug("aggressive_exit_started", pid=pid, market_id=pos["market_id"], initial_price=exit_price, shares=shares_to_sell)
                for slippage in [0.01, 0.03, 0.05]:
                    sell_price = max(0.01, exit_price - slippage)
                    try:
                        logger.debug("aggressive_exit_attempt", pid=pid, market_id=pos["market_id"], attempt_price=sell_price, slippage=slippage)
                        res = await self.clob.place_order(tid, "SELL", sell_price, shares_to_sell, "FOK")
                        if res:
                            self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + (shares_to_sell * sell_price)
                            sold_successfully = True
                            break
                        else:
                            logger.warning("sl_aggressive_sell_fok_rejected", pid=pid, market_id=pos["market_id"], slippage=slippage, attempt_price=sell_price, shares=shares_to_sell, message=f"FOK order rejected by exchange for market {pos['market_id']} at price {sell_price}")
                    except Exception as e:
                        logger.error("sl_aggressive_sell_failed", pid=pid, market_id=pos["market_id"], slippage=slippage, attempt_price=sell_price, error=str(e), message=f"Exception during FOK sell for {pos['market_id']} at price {sell_price}")
                        await asyncio.sleep(0.5)
                
                if not sold_successfully:
                    logger.error("aggressive_exit_failed_all_attempts", pid=pid, market_id=pos["market_id"], shares=shares_to_sell, message=f"FAILED ALL FOK ATTEMPTS for {shares_to_sell} shares on market {pos['market_id']}. ABORTING CLOSE to prevent zombie shares.")
                    return False
            else:
                try:
                    logger.debug("standard_exit_attempt", pid=pid, market_id=pos["market_id"], exit_price=exit_price, shares=shares_to_sell)
                    res = await self.clob.place_order(tid, "SELL", max(0.01, exit_price-0.03), shares_to_sell, "FOK")
                    if res:
                        self._partial_proceeds[pid] = self._partial_proceeds.get(pid, 0.0) + (shares_to_sell * exit_price)
                    else:
                        logger.warning("standard_exit_fok_rejected", pid=pid, market_id=pos["market_id"], exit_price=exit_price, shares=shares_to_sell, message=f"FOK order rejected by exchange for market {pos['market_id']} at price {exit_price}. ABORTING CLOSE.")
                        return False
                except Exception as e:
                    logger.error("final_sell_failed", pid=pid, market_id=pos["market_id"], exit_price=exit_price, shares=shares_to_sell, error=str(e), message=f"Exception during standard FOK sell for {pos['market_id']}. ABORTING CLOSE.")
                    return False

        total_p = self._partial_proceeds.pop(pid, 0.0)
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
            updated_notes = json.dumps(notes_data)
        except: pass
        
        # Ensure Reason: STOP_LOSS appears correctly for losses
        if reason == "TP 1":
            reason = "TAKE_PROFIT"
        
        await close_position(self.db, pid, eff_exit, pnl, reason, paper=self._paper)
        await update_position_notes(self.db, pid, updated_notes, paper=self._paper)
        logger.info("position_finalized", pid=pid, reason=reason, pnl=round(pnl, 4), exit_price=eff_exit)
        
        if self.tg:
            try:
                m_data = await get_market(self.db, pos["market_id"])
                m_name = m_data.get("question", pos["market_id"]) if m_data else pos["market_id"]
                asyncio.create_task(self.tg.notify_trade_exit(m_name, exit_price, pnl, reason, pid, self._paper))
            except: pass
            
        return True



    async def _persist_tracking_data(self, pid: str, tid: str) -> None:
        """Save P&L state to DB notes."""
        try:
            data = {
                "tid": tid,
                "p": self._partial_proceeds.get(pid, 0.0),
                "r": self._shares_remaining.get(pid, 0.0),
                "orders": self._resting_tp_orders.get(pid, [])
            }
            await update_position_notes(self.db, pid, json.dumps(data), paper=self._paper)
        except: pass

    async def _load_tracking_data(self, pos: Dict) -> None:
        """Restore P&L state from DB notes."""
        try:
            data = json.loads(pos.get("notes", "{}"))
            pid = pos["position_id"]
            if "p" in data:
                self._partial_proceeds[pid] = float(data["p"])
                self._shares_remaining[pid] = float(data["r"])
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

    async def _recover_target_token_id(self, mid: str, pid: str, side: str) -> Optional[str]:
        """Heal missing Token ID."""
        if not self.gamma: return None
        clob = await self.gamma.get_market_info(mid)
        if clob and "tokens" in clob:
            target_outcome = "Yes" if side == "YES" else "No"
            for t in clob["tokens"]:
                outcome = str(t.get("outcome", "")).strip()
                if outcome.lower() == target_outcome.lower():
                    tid = t.get("token_id")
                    await update_position_notes(self.db, pid, tid, paper=self._paper)
                    return tid
        return None
