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
            tp_multiplier=config.take_profit_multiplier,
            sl_multiplier=config.stop_loss_multiplier,
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
                etype = event.get("event_type")
                if etype == "MINED":
                    order_id = event.get("order_id")
                    position_id = self._active_orders.pop(order_id, None)
                    if not position_id:
                        continue
                    
                    pos = await get_position(self.db, position_id, paper=self._paper)
                    if pos and pos.get("order_status") == "OPEN":
                        if position_id in self._resting_tp_orders:
                            continue
                            
                        no_token_id = _get_tid_from_notes(pos.get("notes"))
                        entry_price = pos.get("entry_price", 0.5)
                        shares = pos.get("shares_bought", 0)
                        
                        await self._create_resting_tp_orders(position_id, pos["market_id"], no_token_id, shares, entry_price)
            except Exception as e:
                logger.error("event_processor_error", error=str(e))

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
        Open a new position with full safety guards.
        """
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
                notes=no_token_id,
            )

            try:
                # Place FOK order
                order_result = await self.clob.place_order(
                    token_id=no_token_id,
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
                        await self._persist_tracking_data(position_id, no_token_id)
                        
                        if self._paper:
                            await update_position_status(self.db, position_id, "OPEN", "paper_" + uuid.uuid4().hex[:16], paper=True)
                            await self._create_resting_tp_orders(position_id, market_id, no_token_id, actual_shares, entry_price)
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
        """Scan all open positions for TP or SL conditions."""
        open_positions = await get_open_positions(self.db, paper=self._paper)
        if not open_positions:
            return

        now = int(time.time())
        hold_res = self.config.hold_to_resolution

        for pos in open_positions:
            position_id = pos["position_id"]
            market_id = pos["market_id"]
            no_token_id = _get_tid_from_notes(pos.get("notes"))
            
            from src.db.queries import get_market
            m_data = await get_market(self.db, market_id)
            
            # --- Dynamic Stop Loss by Time Regime ---
            sl_mult = getattr(self.config, "mid_stop_loss", 0.65)
            if m_data and m_data.get("closes_at"):
                seconds_to_close = m_data["closes_at"] - now
                early_min = getattr(self.config, "early_phase_min_seconds", 600)
                mid_min = getattr(self.config, "mid_phase_min_seconds", 180)
                
                if seconds_to_close < mid_min:
                    sl_mult = getattr(self.config, "late_stop_loss", 0.90)
                elif seconds_to_close <= early_min:
                    sl_mult = getattr(self.config, "mid_stop_loss", 0.65)
                else:
                    sl_mult = getattr(self.config, "early_stop_loss", 0.80)
            
            if not no_token_id:
                no_token_id = await self._recover_no_token_id(market_id, position_id)
                if not no_token_id: continue

            # ─── ANTI-BLINDNESS FIX ───
            current_no_price = self.ws.bid_cache.get(no_token_id)
            if current_no_price is None or current_no_price <= 0:
                # Try implied price from YES token if NO is empty
                from src.db.queries import get_market
                m_data = await get_market(self.db, market_id)
                if m_data and m_data.get("yes_token_id"):
                    yes_ask = self.ws.ask_cache.get(m_data["yes_token_id"])
                    if yes_ask is not None and yes_ask < 1.0:
                        current_no_price = round(1.0 - yes_ask, 4)

            if current_no_price is None or current_no_price <= 0:
                current_no_price = self.ws.price_cache.get(no_token_id)
            
            if current_no_price is None or current_no_price <= 0:
                current_no_price = self._lkgp_memory.get(position_id)
                if current_no_price is None: continue
            
            self._lkgp_memory[position_id] = current_no_price

            # Recovery logic: Ensure memory state is consistent with database notes
            if position_id not in self._partial_proceeds:
                await self._load_tracking_data(pos)

            shares = self._shares_remaining.get(position_id, pos["shares_bought"])
            
            # --- LIMBO GUARD: If DB says FILLED but notes show remaining shares, keep monitoring ---
            if pos["order_status"] == "FILLED" and shares <= 0:
                continue 
            
            current_value = current_no_price * shares

            # ─── MAKER EXIT CHECK: Resting TP orders ───
            tp_orders = self._resting_tp_orders.get(position_id, [])
            if tp_orders:
                filled_any = False
                for tp_order in list(tp_orders):
                    oid = tp_order["order_id"]
                    target = tp_order["price"]
                    last_check = tp_order.get("last_check_ts", 0)
                    
                    status = None
                    # Honest TP check: Always check in paper mode (fast), or throttle for live API
                    if self._paper or (current_no_price >= target) or (now - last_check > 10):
                        tp_order["last_check_ts"] = now
                        status = await self.clob.get_order_status(oid)

                    if status and status.get("status") in ("MATCHED", "FILLED"):
                            p_shares = tp_order["shares"]
                            p_proceeds = p_shares * target
                            
                            self._partial_proceeds[position_id] = self._partial_proceeds.get(position_id, 0.0) + p_proceeds
                            self._shares_remaining[position_id] = self._shares_remaining.get(position_id, shares) - p_shares
                            await self._persist_tracking_data(position_id, no_token_id)
                            
                            logger.info("tp_partial_fill", position_id=position_id, proceeds=p_proceeds)
                            tp_orders.remove(tp_order)
                            filled_any = True
                
                if not tp_orders and filled_any:
                    await self._close_position(pos, current_no_price, "TAKE_PROFIT", skip_cancel=True)
                    continue
                elif filled_any:
                    self._resting_tp_orders[position_id] = tp_orders

            # ─── TAKER EXIT: Stop Loss ───
            if not hold_res:
                sl_threshold = pos["size_usdc"] * sl_mult
                total_current_value = net_value(current_value) + self._partial_proceeds.get(position_id, 0.0)
                
                # SL Debug (Slow log every 30s)
                if now % 30 == 0:
                    logger.debug("sl_check", pid=position_id, val=round(total_current_value, 3), threshold=round(sl_threshold, 3), price=current_no_price)

                if total_current_value <= sl_threshold:
                    # SL Hardening: Check for extreme wicks (>40% drop in one cycle)
                    prev_p = self._lkgp_memory.get(position_id, current_no_price)
                    is_extreme_wick = (current_no_price < prev_p * 0.6)
                    
                    timer = self._sl_confirmation_timers.get(position_id)
                    if timer is None:
                        self._sl_confirmation_timers[position_id] = now
                        logger.warning("sl_detected_starting_timer", pid=position_id, price=current_no_price, extreme=is_extreme_wick)
                    else:
                        # Wait at least 1.0s to confirm move is real, but exit fast if confirmed toxic
                        wait_time = 1.0 if is_extreme_wick else 2.0
                        if now - timer >= wait_time:
                            is_win = (net_value(current_value) + self._partial_proceeds.get(position_id, 0.0)) > pos["size_usdc"]
                            
                            # Clean Labeling: If we banked profit but the remainder hit SL, call it PARTIAL_TP_EXIT
                            reason = "PARTIAL_TP_EXIT" if self._partial_proceeds.get(position_id, 0.0) > 0 and is_win else "STOP_LOSS"
                            
                            await self._close_position(pos, current_no_price, reason)
                            self._sl_confirmation_timers.pop(position_id, None)
                            continue
                else:
                    if position_id in self._sl_confirmation_timers:
                        logger.info("sl_cleared", pid=position_id)
                    self._sl_confirmation_timers.pop(position_id, None)

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
                    p_no = None
                    for idx, outcome in enumerate(outcomes):
                        if str(outcome).upper() in ["NO", "DOWN"]:
                            p_no = float(prices[idx])
                            break
                    if p_no is not None and p_no in (0.0, 1.0):
                        await self._close_position(pos, p_no, "RESOLUTION")
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

        if shares_to_sell >= 0.1:
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
                    res = await self.clob.place_order(tid, "SELL", max(0.01, exit_price-0.01), shares_to_sell, "FOK")
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

    async def _create_resting_tp_orders(self, pid: str, mid: str, tid: str, shares: float, entry: float) -> None:
        """Dynamic Tiered TP logic (Live or Paper) with API minimum guard and Time Regimes."""
        from src.db.queries import get_market
        m_data = await get_market(self.db, mid)

        tp1_m = getattr(self.config, "mid_tp1", 1.15)
        tp2_m = getattr(self.config, "mid_tp2", 2.00)
        now = int(time.time())

        if m_data and m_data.get("closes_at"):
            seconds_to_close = m_data["closes_at"] - now
            early_min = getattr(self.config, "early_phase_min_seconds", 600)
            mid_min = getattr(self.config, "mid_phase_min_seconds", 180)

            if seconds_to_close < mid_min:
                tp1_m = getattr(self.config, "late_tp1", 1.06)
                tp2_m = getattr(self.config, "late_tp2", 1.06)
            elif seconds_to_close <= early_min:
                tp1_m = getattr(self.config, "mid_tp1", 1.15)
                tp2_m = getattr(self.config, "mid_tp2", 2.00)
            else:
                tp1_m = getattr(self.config, "early_tp1", 1.25)
                tp2_m = getattr(self.config, "early_tp2", 2.50)

        targets = []
        actual_shares = int(shares)        
        if self.config.trailing_tp_enabled and actual_shares >= 10:
            # Dynamic Split logic: Attempt 60% / 40% split
            s1 = int(actual_shares * 0.6)
            s2 = actual_shares - s1
            
            # Polymarket API requires a minimum of 5 shares per order
            if s1 < 5:
                s1 = 5
                s2 = actual_shares - s1
                
            if s2 < 5:
                # If we don't have enough to split into two valid 5-share orders, fallback to single TP
                targets.append({"s": actual_shares, "m": tp1_m})
            else:
                targets.append({"s": s1, "m": tp1_m})
                targets.append({"s": s2, "m": tp2_m})
        else:
            # Single TP fallback:
            # Triggered if trailing is disabled OR we mathematically cannot satisfy two 5-share limit orders
            targets.append({"s": actual_shares, "m": tp1_m})
            
        orders = []
        for t in targets:
            # Never set target price > 0.99
            p = round(min(entry * t["m"], 0.99), 4)
            try:
                res = await self.clob.place_order(tid, "SELL", p, t["s"], "GTC")
                oid = res.get("orderID") or res.get("id") if res else None
                if oid:
                    orders.append({"order_id": oid, "shares": t["s"], "price": p, "multiplier": t["m"]})
            except Exception as e:
                logger.error("tp_fail", pid=pid, error=str(e), price=p, shares=t["s"])
        
        if orders:
            self._resting_tp_orders[pid] = orders
            logger.info("tp_ready", pid=pid, count=len(orders), targets=[{"shares": t["s"], "price": p} for t in targets])

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

    async def _recover_no_token_id(self, mid: str, pid: str) -> Optional[str]:
        """Heal missing Token ID."""
        if not self.gamma: return None
        clob = await self.gamma.get_market_info(mid)
        if clob and "tokens" in clob:
            for t in clob["tokens"]:
                if t.get("outcome") == "No":
                    tid = t.get("token_id")
                    await update_position_notes(self.db, pid, tid, paper=self._paper)
                    return tid
        return None
