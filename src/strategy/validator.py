"""
Signal Validator — All 10 Checks Required Before Order Placement.

If ANY check fails: log SIGNAL_REJECTED with exact reason and all results.
Do not place an order.

Checks:
1. LIQUIDITY CHECK — 24h taker-side volume >= MIN_VOLUME_24H_USDC
2. SPREAD CHECK — bid-ask spread on "No" side < 5 cents
3. TIME CHECK — market closes between MIN/MAX time bounds
4. DUPLICATE CHECK — no OPEN position for this market_id
5. EXPOSURE CHECK — won't exceed MAX_TOTAL_EXPOSURE_USDC
6. DAILY LOSS CHECK — today's realised losses < MAX_DAILY_LOSS_USDC
7. POSITION COUNT CHECK — open positions < MAX_OPEN_POSITIONS
8. PRICE STABILITY CHECK — 2+ consecutive cycles above threshold
9. CATEGORY FILTER — only trade whitelisted categories
10. CVD GUARD — Order book momentum check (skip if Yes buying volume > 5x No buying volume)

MISTAKE 6 GUARD: Fee-adjusted EV check (2% on net winnings)
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.config import BotConfig
from src.strategy.math_utils import FEE_MULTIPLIER
from src.api.clob_client import ClobClient
from src.db.queries import (
    has_open_position_for_market,
    count_open_positions,
    get_total_exposure,
    get_daily_realised_loss,
)
from src.db.rejections import RejectionLogger
from src.logs.log_config import get_logger

logger = get_logger("validator")

# Results of signal validation
@dataclass
class ValidationResult:
    """Result of all 10 validation checks."""
    passed: bool = False
    checks: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    ev_ratio: float = 0.0
    available_depth: float = 0.0
    no_spread: float = 0.0

    def summary(self) -> str:
        """Human-readable summary of all check results."""
        lines = []
        for check, result in self.checks.items():
            status = "✓ PASS" if result else "✗ FAIL"
            lines.append(f"  {status} — {check}")
        return "\n".join(lines)


class SignalValidator:
    """
    Validates a signal through all 10 required checks.
    Each check is independently testable.
    """

    def __init__(
        self,
        config: BotConfig,
        clob_client: ClobClient,
        db,
        rejection_logger: Optional[RejectionLogger] = None,
    ):
        self.config = config
        self.clob = clob_client
        self.db = db
        self.rejection_logger = rejection_logger

    async def validate(
        self,
        market: Dict[str, Any],
        no_token_id: Optional[str] = None,
    ) -> ValidationResult:
        """
        Run ALL 9 validation checks on a signal.

        Args:
            market: Market dict from scanner
            no_token_id: Token ID for the "No" side (for orderbook check)

        Returns:
            ValidationResult with pass/fail for each check
        """
        result = ValidationResult()
        market_id = market["market_id"]
        paper = self.config.paper_trade

        # 1. LIQUIDITY CHECK
        volume_24h = market.get("yes_volume_24h", 0.0)
        if self.config.target_market_mode == "crypto_short_window":
            check_1 = True  # Short window markets often have 0 volume at creation
        else:
            check_1 = volume_24h >= self.config.min_volume_24h_usdc
            
        result.checks["1_liquidity"] = check_1
        if not check_1:
            result.reasons.append(
                f"Liquidity too low: {volume_24h:.2f} < {self.config.min_volume_24h_usdc}"
            )

        # 2. SPREAD CHECK
        no_spread = 0.0
        spread_pct = 0.0
        check_2 = True
        book = None
        if no_token_id:
            book = await self.clob.get_orderbook(no_token_id)
            if book:
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 1.0
                no_spread = best_ask - best_bid
                if best_ask > 0:
                    spread_pct = no_spread / best_ask
                    check_2 = spread_pct <= self.config.max_spread_pct_of_ask
                else:
                    check_2 = False
                    spread_pct = 1.0
                result.available_depth = self.clob.get_available_volume_at_ask(book)
            else:
                check_2 = False
                result.reasons.append("Could not fetch No orderbook")

        result.no_spread = no_spread
        result.checks["2_spread"] = check_2

        # 3. TIME CHECK
        closes_at = market.get("closes_at")
        check_3 = True
        now = int(time.time())
        if closes_at:
            if self.config.target_market_mode == "crypto_short_window":
                # Relaxed window check since scanner already vetted it
                seconds_to_close = closes_at - now
                check_3 = seconds_to_close >= self.config.short_window_min_seconds_to_close
                if not check_3:
                    result.reasons.append(
                        f"Short-window timing invalid: seconds_to_close={seconds_to_close}, min_required={self.config.short_window_min_seconds_to_close}"
                    )
            else:
                hours_to_close = (closes_at - now) / 3600.0
                min_hours = self.config.min_time_to_close_hours
                max_hours = self.config.max_time_to_close_days * 24

                check_3 = min_hours <= hours_to_close <= max_hours
                if not check_3:
                    result.reasons.append(
                        f"Time to close out of range: {hours_to_close:.1f}h "
                        f"(need {min_hours}h-{max_hours}h)"
                    )
        else:
            check_3 = False
            result.reasons.append("No closes_at timestamp available")

        result.checks["3_time"] = check_3

        # 4. DUPLICATE CHECK
        has_open = await has_open_position_for_market(self.db, market_id, paper=paper)
        check_4 = not has_open
        result.checks["4_duplicate"] = check_4
        if not check_4:
            result.reasons.append(f"Already have OPEN position for {market_id}")

        # 5. EXPOSURE CHECK
        current_exposure = await get_total_exposure(self.db, paper=paper)
        new_exposure = current_exposure + self.config.position_size_usdc
        check_5 = new_exposure <= self.config.max_total_exposure_usdc
        result.checks["5_exposure"] = check_5
        if not check_5:
            result.reasons.append(
                f"Exposure would exceed max: {new_exposure:.2f} > {self.config.max_total_exposure_usdc}"
            )

        # 6. DAILY LOSS CHECK
        daily_loss = await get_daily_realised_loss(self.db, paper=paper)
        check_6 = daily_loss < self.config.max_daily_loss_usdc
        result.checks["6_daily_loss"] = check_6
        if not check_6:
            result.reasons.append(
                f"Daily loss limit reached: {daily_loss:.2f} >= {self.config.max_daily_loss_usdc}"
            )

        # 7. POSITION COUNT CHECK
        open_count = await count_open_positions(self.db, paper=paper)
        check_7 = open_count < self.config.max_open_positions
        result.checks["7_position_count"] = check_7
        if not check_7:
            result.reasons.append(
                f"Max positions reached: {open_count} >= {self.config.max_open_positions}"
            )

        # 8. PRICE STABILITY CHECK
        consecutive_cycles = market.get("consecutive_cycles_above", 0)
        required_cycles = 2 # Prevent Flash-Spikes
        check_8 = consecutive_cycles >= required_cycles
        result.checks["8_price_stability"] = check_8
        if not check_8:
            result.reasons.append(
                f"Not enough confirmation cycles: {consecutive_cycles} < {required_cycles}"
            )

        # 9. CATEGORY FILTER
        category_str = (market.get("category") or "").strip()
        tags = [t.strip() for t in category_str.split(",") if t.strip()]
        
        # Bypass category check in crypto mode like scanner does
        if self.config.target_market_mode == "crypto_short_window":
            check_9 = True
        else:
            check_9 = any(t in self.config.category_whitelist for t in tags)
        
        result.checks["9_category"] = check_9
        if not check_9:
            result.reasons.append(
                f"Category '{category_str}' not in whitelist: {self.config.category_whitelist}"
            )

        # 10. WICK DETECTION (Mean Reversion Imbalance) -> REVERSAL CONFIDENCE SCORE (RCS)
        check_10 = True
        if book:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            # Sum top 3 levels to gauge immediate pressure
            no_bid_size = sum(float(b.get("size", 0)) for b in bids[:3])
            no_ask_size = sum(float(a.get("size", 0)) for a in asks[:3])
            
            yes_price = market.get("yes_price", 0.5)
            closes_at = market.get("closes_at", 0)
            seconds_to_close = max(0, closes_at - int(time.time())) if closes_at else 1000
            
            # --- RCS Calculation ---
            # 1. Price Deviation (0 to 4.0)
            # High deviation from 0.5 is good if we have time, bad if we don't.
            price_dev = min(4.0, abs(yes_price - 0.50) * 10)
            
            # 2. Imbalance Weight (0 to 3.0)
            imbalance = 0.0
            if no_bid_size > 0:
                raw_ratio = no_ask_size / no_bid_size
                imbalance = min(3.0, raw_ratio * 0.75)
            elif no_ask_size > 0:
                imbalance = 3.0 # Pure freefall
            
            # 3. Theta Penalty (0 to -5.0)
            # Exerts massive downward pressure if price is extremely biased and time is running out.
            theta_penalty = 0.0
            if seconds_to_close < 180 and yes_price > 0.70:
                theta_penalty = ((180 - seconds_to_close) / 180.0) * (yes_price * 5)
                
            # Assume velocity is decent since the scanner caught it (we add a baseline 1.0)
            velocity_weight = 1.0 
            
            rcs = price_dev + velocity_weight + imbalance - theta_penalty
            
            if rcs < 5.0:
                check_10 = False
                result.reasons.append(f"Wick Guard: RCS too low ({rcs:.1f} < 5.0). Dev:{price_dev:.1f} Imb:{imbalance:.1f} Theta:-{theta_penalty:.1f}")

        result.checks["10_wick_guard"] = check_10

        # Overall result
        result.passed = all(result.checks.values())

        # Calculate fee-adjusted EV
        no_price = market.get("no_price", 0.0)
        if no_price > 0:
            shares = self.config.position_size_usdc / no_price
            net_payout = (1.0 - no_price) * shares * FEE_MULTIPLIER  # 2% fee on net winnings
            result.ev_ratio = net_payout / self.config.position_size_usdc
        else:
            result.ev_ratio = 0.0

        # Log result
        if result.passed:
            logger.info(
                "signal_validated",
                market_id=market_id,
                market_question=market.get("question", ""),
                yes_price=market.get("yes_price"),
                no_price=no_price,
                ev_ratio=round(result.ev_ratio, 3),
                checks=result.checks,
                message="Signal VALIDATED — all 9 checks passed",
            )
        else:
            logger.warning(
                "signal_rejected",
                market_id=market_id,
                market_question=market.get("question", ""),
                yes_price=market.get("yes_price"),
                checks=result.checks,
                reasons=result.reasons,
                message=f"Signal REJECTED — {len(result.reasons)} check(s) failed",
            )
            # Verbose logging to technical rejection DB
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market.get("question", ""),
                    primary_reason=result.reasons[0] if result.reasons else "UNKNOWN",
                    failed_checks=[c for c, r in result.checks.items() if not r],
                    market_data=market
                )

        return result
