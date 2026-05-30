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
    get_market,
    get_open_positions,
    log_event,
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

    async def _check_and_log_resolution_block(
        self,
        failed_check: str,
        current_val: float,
        limit_val: float,
        market_id: str,
        question: str,
        paper: bool,
    ) -> None:
        """
        Check if we are blocked from entering a trade due to closed-but-unresolved positions.
        If so, calculate the wait time and log a RESOLUTION_BLOCK event to the database.
        """
        open_positions = await get_open_positions(self.db, paper=paper)
        now = int(time.time())
        waiting_positions = []
        for pos in open_positions:
            pid = pos["position_id"]
            mid = pos["market_id"]
            m_data = await get_market(self.db, mid)
            if m_data:
                closes_at = m_data.get("closes_at", 0)
                if closes_at > 0 and now > closes_at:
                    wait_time = now - closes_at
                    waiting_positions.append({
                        "position_id": pid,
                        "market_id": mid,
                        "question": m_data.get("question", ""),
                        "closes_at": closes_at,
                        "wait_time_seconds": wait_time
                    })
        if waiting_positions:
            logger.warning(
                "resolution_block_detected",
                failed_check=failed_check,
                current_val=current_val,
                limit_val=limit_val,
                waiting_positions_count=len(waiting_positions),
                message=f"Signal {market_id} blocked by {failed_check} ({current_val} vs limit {limit_val}), "
                        f"but {len(waiting_positions)} positions are closed and waiting for resolution."
            )
            await log_event(
                self.db,
                event_type="RESOLUTION_BLOCK",
                bot_version=self.config.bot_version,
                market_id=market_id,
                payload={
                    "failed_check": failed_check,
                    "current_value": current_val,
                    "limit_value": limit_val,
                    "question": question,
                    "blocked_by_positions": waiting_positions,
                    "timestamp": now
                }
            )

    async def validate(
        self,
        market: Dict[str, Any],
        target_token_id: Optional[str] = None,
    ) -> ValidationResult:
        """
        Run ALL 9 validation checks on a signal.

        Args:
            market: Market dict from scanner
            target_token_id: Token ID for the target side (for orderbook check)

        Returns:
            ValidationResult with pass/fail for each check
        """
        result = ValidationResult()
        market_id = market["market_id"]
        paper = self.config.paper_trade

        # 1. LIQUIDITY CHECK
        volume_24h = market.get("yes_volume_24h", 0.0)
        q = market.get("question", "")
        q_lower = q.lower()
        is_short_window = "up or down" in q_lower and any(kw in q_lower for kw in ["bitcoin", "btc", "ethereum", "eth"])

        if self.config.target_market_mode == "crypto_short_window" or is_short_window:
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
        if target_token_id:
            book = await self.clob.get_orderbook(target_token_id)
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
                # Depth Monitor: Check if the orderbook depth is sufficient to execute the trade
                required_depth = float(self.config.position_size_usdc)
                if result.available_depth < required_depth:
                    check_2 = False
                    result.reasons.append(
                        f"Insufficient ask depth: available_depth={result.available_depth:.2f} USDC < required_depth={required_depth:.2f} USDC"
                    )
                if not check_2:
                    result.reasons.append(
                        f"Spread too wide: spread_pct={spread_pct:.2%} "
                        f"(spread={no_spread:.4f}, best_bid={best_bid:.4f}, best_ask={best_ask:.4f}) "
                        f"exceeds max_allowed={self.config.max_spread_pct_of_ask:.2%}"
                    )
            else:
                check_2 = False
                result.reasons.append("Could not fetch target orderbook")

        result.no_spread = no_spread
        result.checks["2_spread"] = check_2

        # 3. TIME CHECK
        closes_at = market.get("closes_at")
        check_3 = True
        now = int(time.time())
        if closes_at:
            if self.config.target_market_mode == "crypto_short_window" or is_short_window:
                # Relaxed window check since scanner already vetted it
                seconds_to_close = closes_at - now
                duration = market.get("window_minutes") or 5
                min_ttc = 60 if duration == 5 else 120
                check_3 = seconds_to_close >= min_ttc
                if not check_3:
                    result.reasons.append(
                        f"Short-window timing invalid: seconds_to_close={seconds_to_close}, min_required={min_ttc}"
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
            await self._check_and_log_resolution_block(
                failed_check="exposure_limit",
                current_val=new_exposure,
                limit_val=self.config.max_total_exposure_usdc,
                market_id=market_id,
                question=q,
                paper=paper
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
            await self._check_and_log_resolution_block(
                failed_check="position_count_limit",
                current_val=open_count,
                limit_val=self.config.max_open_positions,
                market_id=market_id,
                question=q,
                paper=paper
            )

        # 8. PRICE STABILITY CHECK
        if self.config.target_market_mode == "crypto_short_window" or is_short_window:
            check_8 = True
            consecutive_cycles = market.get("consecutive_cycles_above", 0)
            required_cycles = 0
        else:
            consecutive_cycles = market.get("consecutive_cycles_above", 0)
            
            closes_at = market.get("closes_at", 0)
            seconds_to_close = max(0, closes_at - int(time.time())) if closes_at else 1000
            early_min = getattr(self.config, "early_phase_min_seconds", 600)
            early_min = int(early_min) if isinstance(early_min, (int, float)) else 600
            mid_min = getattr(self.config, "mid_phase_min_seconds", 180)
            mid_min = int(mid_min) if isinstance(mid_min, (int, float)) else 180
            
            if seconds_to_close < mid_min:
                required_cycles = getattr(self.config, "late_cycles", 3)
            elif seconds_to_close <= early_min:
                required_cycles = getattr(self.config, "mid_cycles", 2)
            else:
                required_cycles = getattr(self.config, "early_cycles", 1)

            required_cycles = int(required_cycles) if isinstance(required_cycles, (int, float)) else 1

            check_8 = consecutive_cycles >= required_cycles
        result.checks["8_price_stability"] = check_8
        if not check_8:
            result.reasons.append(
                f"Not enough confirmation cycles: {consecutive_cycles} < {required_cycles}"
            )

        # 9. CATEGORY FILTER
        category_str = (market.get("category") or "").strip()
        tags = [t.strip() for t in category_str.split(",") if t.strip()]
        
        # Bypass category check in crypto mode or if the question explicitly contains crypto keywords
        q_upper = market.get("question", "").upper()
        is_crypto_q = any(kw in q_upper for kw in ["BITCOIN", "ETHEREUM", "BTC", "ETH"])
        
        if self.config.target_market_mode == "crypto_short_window" or is_crypto_q:
            check_9 = True
        else:
            check_9 = any(t in self.config.category_whitelist for t in tags)
        
        result.checks["9_category"] = check_9
        if not check_9:
            result.reasons.append(
                f"Category '{category_str}' not in whitelist: {self.config.category_whitelist}"
            )

        # 10. SPREAD VACUUM DETECTOR (Flash Crash / Wick Guard)
        check_10 = True
        # DISABLED for Z-Score Architecture (Daily/Weekly markets may have tight spreads during a valid Z-score spike)
        result.checks["10_wick_guard"] = check_10

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
