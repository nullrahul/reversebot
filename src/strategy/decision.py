import re
import asyncio
from typing import Any, Dict, Optional, Tuple
import time

from config.config import BotConfig
from src.strategy.math_utils import expected_value, slippage_depth_required, FEE_MULTIPLIER
from src.api.clob_client import ClobClient
from src.strategy.validator import SignalValidator, ValidationResult
from src.strategy.position_mgr import PositionManager
from src.db.queries import log_event, has_traded_market, get_open_positions, get_market
from src.db.rejections import RejectionLogger
from src.logs.log_config import get_logger

logger = get_logger("decision")

# Global Entry Lock and Timestamp to prevent rapid-fire burst entries
_global_entry_lock = asyncio.Lock()
_last_entry_time = 0.0

def get_core_context(question: str) -> str:
    """Extract the core event context by stripping dates, times, and window markers."""
    # 1. Remove months and dates (April 15, 2026, etc.)
    q = re.sub(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b', '', question, flags=re.I)
    
    # 2. Remove time ranges (8:25AM-8:30AM ET, 10:00AM, etc.)
    q = re.sub(r'\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))?\s*(?:ET|UTC|UTC\d+)?', '', q, flags=re.I)
    
    # 3. Remove numeric dates (04/15/26) and standalone years (2026)
    q = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '', q)
    q = re.sub(r'\b202\d\b', '', q)
    
    # 4. Remove 'Up or Down' window markers often at the end
    q = re.sub(r'\s+on\s*$', '', q, flags=re.I)
    q = re.sub(r'\s+by\s*$', '', q, flags=re.I)
    q = re.sub(r'\?$', '', q)
    
    # 5. Clean up and normalize
    q = re.sub(r'^Will\s+', '', q, flags=re.I)
    return ' '.join(q.split()).strip().lower()


class DecisionEngine:
    """
    Orchestrates the full trading decision pipeline:
    Signal → Validate → EV → Slippage → Execute
    """

    def __init__(
        self,
        config: BotConfig,
        validator: SignalValidator,
        position_mgr: PositionManager,
        clob_client: ClobClient,
        db,
        rejection_logger: Optional[RejectionLogger] = None,
        scanner = None,
    ):
        self.config = config
        self.validator = validator
        self.position_mgr = position_mgr
        self.clob = clob_client
        self.db = db
        self.rejection_logger = rejection_logger
        self.scanner = scanner

    async def process_signal(
        self,
        market: Dict[str, Any],
        no_token_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Process a confirmed scanner signal through the full decision pipeline.

        Args:
            market: Market dict with yes_price, no_price, etc.
            no_token_id: Token ID for the "No" side

        Returns:
            (success: bool, reason: str)
        """
        global _last_entry_time
        
        market_id = market["market_id"]
        market_question = market.get("question", "")
        yes_price = market.get("yes_price", 0.0)
        no_price = market.get("no_price", 0.0)
        new_closes_at = market.get("closes_at") or 0

        # ─── Step -3: Pre-Entry Time-to-Close Guard (Change 5 — decision layer) ───
        # The scanner already checks this, but there's a race: by the time the
        # signal reaches here, we may be closer to expiry. Re-check to be safe.
        if new_closes_at > 0:
            secs_left = new_closes_at - int(time.time())
            min_ttc = getattr(self.config, "min_seconds_to_close_pre_entry", 180)
            if secs_left < min_ttc:
                logger.info(
                    "decision_ttc_guard",
                    market_id=market_id,
                    secs_left=secs_left,
                    min_required=min_ttc,
                    message=f"Rejecting: only {secs_left}s left on market (need {min_ttc}s)"
                )
                return False, f"Market too close to expiry ({secs_left}s < {min_ttc}s)"

        # ─── Step -2.1: Final Threshold Guard (Dogecoin Fix) ───
        # Re-fetch absolute latest price from WebSocket cache
        yes_token_id = market.get("yes_token_id")
        if yes_token_id and self.position_mgr.ws:
            latest_yes = self.position_mgr.ws.ask_cache.get(yes_token_id) or self.position_mgr.ws.price_cache.get(yes_token_id)
            if latest_yes is not None:
                if latest_yes >= self.config.yes_bias_max_threshold:
                    logger.info(
                        "toxic_entry_prevented",
                        market_id=market_id,
                        latest_yes=latest_yes,
                        max_allowed=self.config.yes_bias_max_threshold,
                        message=f"PRICE SPIKE DETECTED: Latest Yes ({latest_yes}) >= Max Threshold ({self.config.yes_bias_max_threshold}). Rejecting entry."
                    )
                    return False, f"Toxic price detected at entry: {latest_yes} >= {self.config.yes_bias_max_threshold}"
                # Update yes_price to be the absolute latest for downstream logic
                yes_price = latest_yes

        # ─── Step -2: Spread Guard (Max NO Price) ───
        fair_no_price = max(0.01, 1.0 - yes_price)
        
        mult = self.config.max_no_ask_multiplier
        if any(asset.strip().upper() in market_question.upper() for asset in self.config.high_vol_assets):
            mult = self.config.high_vol_ask_multiplier
            
        max_no_entry_price = min(1.0, round(fair_no_price * mult, 4))
        if no_price > max_no_entry_price:
            logger.info(
                "spread_guard_rejected", 
                market_id=market_id, 
                no_price=no_price, 
                yes_price=yes_price, 
                message=f"Spread too wide: NO price ({no_price}) > max allowed ({max_no_entry_price})"
            )
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market_question,
                    primary_reason=f"SPREAD_GUARD_NO_PRICE_{no_price}_GT_{max_no_entry_price}",
                    failed_checks=["spread_multiplier_guard"],
                    market_data=market
                )
            return False, f"NO price ({no_price}) exceeds maximum allowed ({max_no_entry_price})"

        # ─── Step -1: Global Concurrency & Cooldown Guard ───
        if True:
            now = time.time()
            
            asset_keywords = ["bitcoin", "ethereum", "solana", "dogecoin", "xrp", "bnb", "hyperliquid"]
            current_asset = next((kw for kw in asset_keywords if kw in market_question.lower()), "unknown")

            time_since_last = now - _last_entry_time
            cooldown = self.config.global_cooldown
            
            # HARD LIMIT: Global cooldown is now strict.
            if time_since_last < cooldown:
                wait_needed = int(cooldown - time_since_last)
                logger.info(
                    "global_cooldown_active",
                    market_id=market_id,
                    wait_seconds=wait_needed,
                    message=f"Global cooldown active: skipping signal for {wait_needed}s"
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"GLOBAL_COOLDOWN_{wait_needed}S",
                        failed_checks=["global_cooldown"],
                        market_data=market
                    )
                return False, f"Global entry cooldown: {wait_needed}s remaining"

            # ─── Step 0: Ensure we never trade a market more than once ───
            has_traded = await has_traded_market(self.db, market_id, paper=self.config.paper_trade)
            if has_traded:
                logger.info("decision_duplicate_market", market_id=market_id, message="Market already traded. Skipping to prevent duplicates.")
                return False, "Market already traded once"

            # ─── Step 0.2: Hard Limit for Crypto Mode ───
            open_positions = await get_open_positions(self.db, paper=self.config.paper_trade)
            
            # --- FIX: "Double-Spending" Race Condition Guard ---
            current_exposure = sum(p.get("size_usdc", 0.0) for p in open_positions)
            if current_exposure + self.config.position_size_usdc > self.config.max_total_exposure_usdc:
                logger.warning(
                    "max_exposure_guard",
                    current=current_exposure,
                    adding=self.config.position_size_usdc,
                    max=self.config.max_total_exposure_usdc,
                    message="Max total exposure limit reached in decision engine."
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"MAX_EXPOSURE_REACHED_{current_exposure}",
                        failed_checks=["max_exposure_guard"],
                        market_data=market
                    )
                return False, f"Max total exposure reached ({current_exposure} + {self.config.position_size_usdc} > {self.config.max_total_exposure_usdc})"

            if self.config.target_market_mode == "crypto_short_window":
                # Respect .env limit
                if len(open_positions) >= self.config.max_open_positions:
                    logger.warning(
                        "global_limit_reached",
                        count=len(open_positions),
                        max_limit=self.config.max_open_positions,
                        message=f'Hard limit of {self.config.max_open_positions} concurrent positions reached'
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"HARD_LIMIT_CONCURRENCY_{len(open_positions)}",
                            failed_checks=["global_concurrency_limit"],
                            market_data=market
                        )
                    return False, f"Global concurrency limit ({self.config.max_open_positions}) reached"

            # ─── Step 0.5: Similarity Guard (Cluster Protection) ───
            # In crypto_short_window mode, we allow different time windows for the same asset,
            # BUT we enforce a strict 1-trade-per-asset cap to prevent cluster doom streaks.
            new_core = get_core_context(market_question)
            
            if self.config.target_market_mode == "crypto_short_window":
                for pos in open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    existing_q = existing_m.get("question", "").lower()
                    existing_asset = next((kw for kw in asset_keywords if kw in existing_q), "unknown_old")
                    
                    # If the core asset is the same, reject to prevent the "Sequential Doom" pattern
                    if current_asset != "unknown" and current_asset == existing_asset:
                        logger.warning(
                            "asset_cap_rejected",
                            market_id=market_id,
                            asset=current_asset,
                            message=f"Cluster risk: already holding an open position for '{current_asset}'"
                        )
                        if self.rejection_logger:
                            await self.rejection_logger.log_rejection(
                                market_id=market_id,
                                question=market_question,
                                primary_reason=f"ASSET_CAP_REJECTED_{current_asset}",
                                failed_checks=["asset_correlation_cap"],
                                market_data=market
                            )
                        return False, f"Asset Cap reached for {current_asset}"
            else:
                # Original logic for non-crypto mode
                for pos in open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    
                    existing_core = get_core_context(existing_m["question"])
                    
                    if new_core == existing_core:
                        existing_closes_at = existing_m.get("closes_at") or 0
                        
                        if new_closes_at < existing_closes_at and new_closes_at > 0:
                            logger.info(
                                "similarity_swap",
                                new_id=market_id,
                                old_id=pos["market_id"],
                                context=new_core,
                                message=f"Shorter duration found for '{new_core}'. Closing old and swapping."
                            )
                            await self.position_mgr.manual_close_position(pos["position_id"])
                            break
                        else:
                            logger.warning(
                                "similarity_rejected",
                                market_id=market_id,
                                context=new_core,
                                message=f"Clustered market rejected: already holding shorter duration for '{new_core}'"
                            )
                            return False, f"Correlation risk: duplicate context '{new_core}'"

            # ─── Step 1: Run all 9 validation checks ───
            validation = await self.validator.validate(market, no_token_id)

            if not validation.passed:
                await log_event(
                    self.db,
                    event_type="SIGNAL_REJECTED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    payload={
                        "checks": validation.checks,
                        "reasons": validation.reasons,
                        "yes_price": yes_price,
                    },
                )
                return False, f"Validation failed: {'; '.join(validation.reasons)}"

            # ─── Step 2: Fee-adjusted EV check ───
            if no_price <= 0:
                return False, "No price is zero or negative"

            shares = self.config.position_size_usdc / no_price
            
            # MISTAKE 6 GUARD: 2% fee on net winnings
            net_payout = (1.0 - no_price) * shares * FEE_MULTIPLIER
                
            ev_ratio = net_payout / self.config.position_size_usdc

            if ev_ratio < self.config.min_ev_threshold:
                logger.warning(
                    "insufficient_ev",
                    market_id=market_id,
                    ev_ratio=round(ev_ratio, 3),
                    min_required=self.config.min_ev_threshold,
                    no_price=no_price,
                    message=f"EV too low: {ev_ratio:.3f} < {self.config.min_ev_threshold}",
                )
                await log_event(
                    self.db,
                    event_type="SIGNAL_REJECTED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    payload={"reason": "INSUFFICIENT_EV", "ev_ratio": ev_ratio},
                )
                return False, f"Insufficient EV: {ev_ratio:.3f} < {self.config.min_ev_threshold}"
            # ─── Step 3: Slippage guard (DISABLED per User Request) ───
            if False: # validation.available_depth > 0:
                required_depth = slippage_depth_required(self.config.position_size_usdc, self.config.slippage_depth_multiplier)
                if validation.available_depth < required_depth:
                    logger.warning(
                        "insufficient_depth",
                        market_id=market_id,
                        available_depth=round(validation.available_depth, 2),
                        required_depth=round(required_depth, 2),
                        message=f"Depth too thin: {validation.available_depth:.2f} < {required_depth:.2f}",
                    )
                    await log_event(
                        self.db,
                        event_type="SIGNAL_REJECTED",
                        bot_version=self.config.bot_version,
                        market_id=market_id,
                        payload={
                            "reason": "INSUFFICIENT_DEPTH",
                            "available": validation.available_depth,
                            "required": required_depth,
                        },
                    )
                    return False, f"Insufficient depth: {validation.available_depth:.2f}"

            # ─── Step 4: Shares calculation & API Minimums ───
            # MISTAKE 3 GUARD: shares = size_usdc / no_price, NOT size_usdc
            # FIX: "Cents Leak" Integer Share Rounding Error
            # Polymarket often enforces whole integer shares. We must explicitly round down (int) 
            # and recalculate the exact USDC size to prevent P&L discrepancy.
            raw_shares = self.config.position_size_usdc / no_price
            shares = float(int(raw_shares))
            current_size_usdc = shares * no_price

            # Enforce Polymarket CLOB minimum order sizes
            order_min_size = market.get("order_min_size", 5.0)

            if shares < order_min_size:
                if self.config.target_market_mode == "crypto_short_window" and self.config.paper_trade:
                    # R&D paper mode: clamp to minimum and UPDATE REAL COST
                    shares = order_min_size
                    current_size_usdc = shares * no_price
                    logger.info(
                        "shares_clamped_to_minimum",
                        market_id=market_id,
                        clamped_to=order_min_size,
                        requested_size=self.config.position_size_usdc,
                        actual_cost=round(current_size_usdc, 3),
                        message=f"Bumping to {order_min_size} shares to meet Polymarket API minimum (Cost: ${current_size_usdc:.2f})",
                    )
                else:
                    logger.warning(
                        "insufficient_shares",
                        market_id=market_id,
                        calculated_shares=round(shares, 2),
                        required_minimum=order_min_size,
                        message=f"Calculated shares ({shares:.2f}) is below API minimum ({order_min_size})",
                    )
                    return False, f"Shares calculated (< {order_min_size}) violates Polymarket API minimum size"
            # ─── Step 5: Execute via Position Manager ───
            async with _global_entry_lock:
                if time.time() - _last_entry_time < self.config.global_cooldown:
                    return False, f"Global Cooldown caught by lock"

                success = await self.position_mgr.open_position(
                    market_id=market_id,
                    no_token_id=market.get("no_token_id"),
                    entry_price=no_price,
                    size_usdc=current_size_usdc,
                    shares=shares,
                )

                if success:
                    _last_entry_time = time.time()
                    await log_event(
                        self.db,
                        event_type="ORDER_PLACED",
                        bot_version=self.config.bot_version,
                        market_id=market_id,
                        payload={
                            "side": "NO",
                            "entry_price": no_price,
                            "shares": shares,
                            "size_usdc": current_size_usdc,
                            "ev_ratio": ev_ratio,
                            "yes_price": yes_price,
                        },
                    )
                    return True, "Order placed successfully"
                else:
                    return False, "Position manager failed to execute order"

