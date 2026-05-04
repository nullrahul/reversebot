import re
import asyncio
from typing import Any, Dict, Optional, Tuple
import time

from config.config import BotConfig
from src.api.clob_client import ClobClient
from src.strategy.validator import SignalValidator, ValidationResult
from src.strategy.position_mgr import PositionManager
from src.db.queries import log_event, has_traded_market, get_open_positions, get_market
from src.logs.log_config import get_logger

logger = get_logger("decision")

# Minimum acceptable fee-adjusted EV ratio
MIN_EV_RATIO = 0.0 # Relaxed for testing

# Slippage guard: require depth >= size * 1.2
SLIPPAGE_DEPTH_MULTIPLIER = 1.2

# Global Entry Lock and Timestamp to prevent rapid-fire burst entries
_global_entry_lock = asyncio.Lock()
_last_entry_time = 0.0

# Minimum seconds to wait between any two successful trade entries
GLOBAL_ENTRY_COOLDOWN_SECONDS = 40

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
    ):
        self.config = config
        self.validator = validator
        self.position_mgr = position_mgr
        self.clob = clob_client
        self.db = db

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

        # ─── Step -2: Spread Guard (Max NO Price) ───
        fair_no_price = max(0.01, 1.0 - yes_price)
        max_no_entry_price = min(1.0, round(fair_no_price * self.config.max_no_ask_multiplier, 4))
        if no_price > max_no_entry_price:
            logger.info(
                "spread_guard_rejected", 
                market_id=market_id, 
                no_price=no_price, 
                yes_price=yes_price, 
                message=f"Spread too wide: NO price ({no_price}) > max allowed ({max_no_entry_price})"
            )
            return False, f"NO price ({no_price}) exceeds maximum allowed ({max_no_entry_price})"

        # ─── Step -1: Global Concurrency & Cooldown Guard ───
        async with _global_entry_lock:
            now = time.time()
            time_since_last = now - _last_entry_time
            if time_since_last < GLOBAL_ENTRY_COOLDOWN_SECONDS:
                wait_needed = int(GLOBAL_ENTRY_COOLDOWN_SECONDS - time_since_last)
                logger.info(
                    "global_cooldown_active",
                    market_id=market_id,
                    wait_seconds=wait_needed,
                    message=f"Global cooldown active: skipping signal for {wait_needed}s"
                )
                return False, f"Global entry cooldown: {wait_needed}s remaining"

            # ─── Step 0: Ensure we never trade a market more than once ───
            has_traded = await has_traded_market(self.db, market_id, paper=self.config.paper_trade)
            if has_traded:
                logger.info("decision_duplicate_market", market_id=market_id, message="Market already traded. Skipping to prevent duplicates.")
                return False, "Market already traded once"

            # ─── Step 0.2: Hard Limit for Crypto Mode ───
            open_positions = await get_open_positions(self.db, paper=self.config.paper_trade)
            if self.config.target_market_mode == "crypto_short_window":
                # Override .env if it's too high for R&D safety
                if len(open_positions) >= 3:
                    logger.warning(
                        "global_limit_reached",
                        count=len(open_positions),
                        message="Hard limit of 3 concurrent positions reached for R&D safety"
                    )
                    return False, "Global concurrency limit (3) reached"

            # ─── Step 0.5: Similarity Guard (Cluster Protection) ───
            # In crypto_short_window mode, we allow different time windows for the same asset,
            # BUT we enforce a strict 1-trade-per-asset cap to prevent cluster doom streaks.
            new_core = get_core_context(market_question)
            
            # New Feature: Asset Cap (1 trade per core asset)
            asset_keywords = ["bitcoin", "ethereum", "solana", "dogecoin", "xrp", "bnb", "hyperliquid"]
            new_asset = next((kw for kw in asset_keywords if kw in market_question.lower()), "unknown")
            
            if self.config.target_market_mode == "crypto_short_window":
                for pos in open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    existing_q = existing_m.get("question", "").lower()
                    existing_asset = next((kw for kw in asset_keywords if kw in existing_q), "unknown_old")
                    
                    # If the core asset is the same, reject to prevent the "Sequential Doom" pattern
                    if new_asset != "unknown" and new_asset == existing_asset:
                        logger.warning(
                            "asset_cap_rejected",
                            market_id=market_id,
                            asset=new_asset,
                            message=f"Cluster risk: already holding an open position for '{new_asset}'"
                        )
                        return False, f"Asset Cap reached for {new_asset}"
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
            net_payout = (1.0 - no_price) * shares * 0.98
                
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

            # ─── Step 3: Slippage guard ───
                required_depth = self.config.position_size_usdc * SLIPPAGE_DEPTH_MULTIPLIER
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
            shares = self.config.position_size_usdc / no_price

            # Enforce Polymarket CLOB minimum order sizes
            order_min_size = market.get("order_min_size", 5.0)
            current_size_usdc = self.config.position_size_usdc

            if shares < order_min_size:
                if self.config.target_market_mode == "crypto_short_window" and self.config.paper_trade:
                    # R&D paper mode: clamp to minimum and UPDATE REAL COST
                    shares = order_min_size
                    current_size_usdc = shares * no_price
                    logger.info(
                        "shares_clamped_to_minimum",
                        market_id=market_id,
                        clamped_to=order_min_size,
                        actual_cost=round(current_size_usdc, 3),
                        message=f"R&D paper mode: clamped to {order_min_size} shares (Cost: ${current_size_usdc:.2f})",
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
                
                return False, "Order value violates API $1 minimum limitation"

            # ─── Step 5: Execute via Position Manager ───
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
                        "size_usdc": self.config.position_size_usdc,
                        "ev_ratio": ev_ratio,
                        "yes_price": yes_price,
                    },
                )
                return True, "Order placed successfully"
            else:
                return False, "Position manager failed to execute order"

                return False, "Order value violates API $1 minimum limitation"

            # ─── Step 5: Execute via Position Manager ───
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
                        "size_usdc": self.config.position_size_usdc,
                        "ev_ratio": ev_ratio,
                        "yes_price": yes_price,
                    },
                )
                return True, "Order placed successfully"
            else:
                return False, "Position manager failed to execute order"

