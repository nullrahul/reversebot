"""
Risk Limits — Daily Loss, Exposure Cap, Position Count for Reverse Bot.

Checked before every order AND in the kill switch monitor loop.

MISTAKE 11 GUARD: MAX_DAILY_LOSS_USDC prevents a bug or bad run
from draining capital with no operator watching.
"""

import time
from typing import Any, Dict, Tuple

from config.config import BotConfig
from src.db.queries import (
    count_open_positions,
    get_total_exposure,
    get_daily_realised_loss,
)
from src.logs.log_config import get_logger

logger = get_logger("limits")


class RiskLimits:
    """
    Enforces position limits, exposure caps, and daily loss limits.
    All checks return (allowed: bool, reason: str).
    """

    def __init__(self, config: BotConfig, db):
        self.config = config
        self.db = db

    async def check_all(self, paper: bool = False) -> Tuple[bool, str]:
        """
        Run all risk checks. Returns (allowed, reason).
        If any check fails, returns False with the reason.
        """
        checks = [
            await self.check_daily_loss(paper),
            await self.check_position_count(paper),
            await self.check_exposure(paper),
        ]

        for allowed, reason in checks:
            if not allowed:
                return False, reason

        return True, "All risk checks passed"

    async def check_daily_loss(self, paper: bool = False) -> Tuple[bool, str]:
        """Check if daily loss limit has been reached."""
        daily_loss = await get_daily_realised_loss(self.db, paper=paper)
        max_loss = self.config.max_daily_loss_usdc

        if daily_loss >= max_loss:
            logger.critical(
                "daily_loss_limit_reached",
                daily_loss=round(daily_loss, 2),
                max_loss=max_loss,
                message=f"DAILY LOSS LIMIT: ${daily_loss:.2f} >= ${max_loss:.2f}",
            )
            return False, f"Daily loss limit reached: ${daily_loss:.2f} >= ${max_loss:.2f}"

        return True, f"Daily loss OK: ${daily_loss:.2f} < ${max_loss:.2f}"

    async def check_position_count(self, paper: bool = False) -> Tuple[bool, str]:
        """Check if max position count has been reached."""
        count = await count_open_positions(self.db, paper=paper)
        max_pos = self.config.max_open_positions

        if count >= max_pos:
            logger.warning(
                "max_positions_reached",
                count=count,
                max_positions=max_pos,
                message=f"Max positions reached: {count} >= {max_pos}",
            )
            return False, f"Max positions reached: {count} >= {max_pos}"

        return True, f"Position count OK: {count} < {max_pos}"

    async def check_exposure(
        self,
        paper: bool = False,
        additional_usdc: float = 0.0,
    ) -> Tuple[bool, str]:
        """Check if adding a trade would exceed max exposure."""
        current = await get_total_exposure(self.db, paper=paper)
        projected = current + additional_usdc
        max_exp = self.config.max_total_exposure_usdc

        if projected > max_exp:
            logger.warning(
                "exposure_limit_exceeded",
                current_exposure=round(current, 2),
                additional=round(additional_usdc, 2),
                projected=round(projected, 2),
                max_exposure=max_exp,
                message=f"Exposure would exceed limit: ${projected:.2f} > ${max_exp:.2f}",
            )
            return False, f"Exposure limit: ${projected:.2f} > ${max_exp:.2f}"

        return True, f"Exposure OK: ${projected:.2f} <= ${max_exp:.2f}"

    async def should_kill_switch(self, paper: bool = False) -> Tuple[bool, str]:
        """
        Check if kill switch should fire based on risk limits.
        This is called from the main monitoring loop.
        """
        daily_loss = await get_daily_realised_loss(self.db, paper=paper)
        if daily_loss >= self.config.max_daily_loss_usdc:
            return True, f"Daily loss ${daily_loss:.2f} >= limit ${self.config.max_daily_loss_usdc:.2f}"

        return False, "No kill trigger from risk limits"
