"""
Shadow Meta-Strategy Logger
============================
Observational system that logs rich context for each profile trade.
Does NOT modify any trading behavior. Pure data collection.

Usage:
    meta_logger = MetaStrategyLogger(db)
    
    # When a profile position is opened:
    await meta_logger.log_trade_opened(position_dict)
    
    # When a profile position resolves:
    await meta_logger.log_trade_resolved(position_id, realised_pnl, closed_at)
"""

import time
from typing import Any, Dict, Optional, Tuple, List
from src.logs.log_config import get_logger
from src.db.queries import get_db_lock, execute_with_retry, commit_with_retry, fetch_all, fetch_one

logger = get_logger("meta_logger")

CROSS_PROFILE_MAP = {
    "cheap_reversal": "stop_loss",
    "stop_loss": "cheap_reversal",
    "reversal_stop_loss": "stop_loss",
    "reversal_yes_cvd_imb_wide": "stop_loss",
    "reversal_yes_cvd_imb": "stop_loss",
    "loss_skip_yes_cvd": "cheap_reversal",
}


class MetaStrategyLogger:
    """
    Shadow Meta-Strategy Logger.
    Queries state context asynchronously and persists snapshots to meta_strategy_log.
    Guaranteed non-blocking and fail-safe: swallowed exceptions log warnings and never raise.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    async def log_trade_opened(self, position: Dict[str, Any]) -> None:
        """
        Extract context metrics for a newly opened position and log into meta_strategy_log.
        """
        try:
            position_id = str(position.get("position_id", ""))
            profile_name = str(position.get("profile_name", ""))
            market_id = str(position.get("market_id", ""))
            symbol = str(position.get("symbol", "BTC"))
            window_minutes = int(position.get("window_minutes", 5))
            opened_at = int(position.get("opened_at", time.time()))
            entry_price = float(position.get("entry_price", 0.50))
            hour_et = int(position.get("hour_et", 0))
            day_of_week = str(position.get("day_of_week", ""))
            is_weekend = int(position.get("is_weekend", 0))

            if not position_id or not profile_name:
                logger.warning("meta_logger_invalid_position_payload", position_id=position_id, profile=profile_name)
                return

            # Step 2a: Compute Sequential State
            seq_wins, seq_losses, seq_outcomes = await self._compute_sequential_state(
                profile_name, symbol, window_minutes
            )

            # Step 2b: Compute Trailing Win Rates (7d and 14d)
            t7_total, t7_wins, t7_wr, avg_entry_7d = await self._compute_trailing_stats(profile_name, opened_at, 7)
            t14_total, t14_wins, t14_wr, _ = await self._compute_trailing_stats(profile_name, opened_at, 14)

            # Breakeven Context
            if avg_entry_7d > 0.0 and avg_entry_7d < 1.0:
                breakeven_wr_7d = avg_entry_7d / (avg_entry_7d + (1.0 - avg_entry_7d) * 0.98)
            else:
                breakeven_wr_7d = 0.5
            edge_over_breakeven_7d = t7_wr - breakeven_wr_7d

            # Step 2c: Compute Cross-Profile Regime
            cross_profile = CROSS_PROFILE_MAP.get(profile_name, "")
            cross_7d_total = 0
            cross_7d_wr = 0.0
            if cross_profile:
                cross_7d_total, _, cross_7d_wr, _ = await self._compute_trailing_stats(cross_profile, opened_at, 7)

            # Step 2d: Derive Regime Signal
            if cross_7d_total < 20:
                regime_signal = "UNKNOWN"
            elif t7_wr > (breakeven_wr_7d + 0.03) and cross_7d_wr < 0.50:
                regime_signal = "REVERSAL_FAVORED"
            elif t7_wr < breakeven_wr_7d and cross_7d_wr > 0.55:
                regime_signal = "TREND_FAVORED"
            else:
                regime_signal = "NEUTRAL"

            # Step 2e: INSERT into meta_strategy_log
            sql = """
                INSERT INTO meta_strategy_log (
                    position_id, profile_name, market_id, symbol, window_minutes, opened_at,
                    seq_consecutive_wins, seq_consecutive_losses, seq_last_5_outcomes,
                    trailing_7d_total, trailing_7d_wins, trailing_7d_wr,
                    trailing_14d_total, trailing_14d_wins, trailing_14d_wr,
                    avg_entry_price_7d, breakeven_wr_7d, edge_over_breakeven_7d,
                    cross_profile_name, cross_profile_7d_wr, cross_profile_7d_total,
                    regime_signal, hour_et, day_of_week, is_weekend
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                position_id, profile_name, market_id, symbol, window_minutes, opened_at,
                seq_wins, seq_losses, seq_outcomes,
                t7_total, t7_wins, t7_wr,
                t14_total, t14_wins, t14_wr,
                avg_entry_7d, breakeven_wr_7d, edge_over_breakeven_7d,
                cross_profile, cross_7d_wr, cross_7d_total,
                regime_signal, hour_et, day_of_week, is_weekend
            )

            if hasattr(self.db, "execute"):
                async with get_db_lock(self.db):
                    await execute_with_retry(self.db, sql, params)
                    if hasattr(self.db, "commit"):
                        await commit_with_retry(self.db)

            logger.info(
                "meta_trade_opened_logged",
                position_id=position_id,
                profile=profile_name,
                seq_outcomes=seq_outcomes,
                trailing_7d_wr=round(t7_wr, 4),
                regime_signal=regime_signal
            )

        except Exception as e:
            logger.warning("meta_logger_open_error", error=str(e))

    async def log_trade_resolved(self, position_id: str, realised_pnl: float, closed_at: int) -> None:
        """
        Update the meta_strategy_log record when a position resolves.
        """
        try:
            trade_won = 1 if realised_pnl > 0.0 else 0
            sql = """
                UPDATE meta_strategy_log
                SET closed_at = ?, realised_pnl = ?, trade_won = ?
                WHERE position_id = ?
            """
            params = (closed_at, realised_pnl, trade_won, position_id)

            if hasattr(self.db, "execute"):
                async with get_db_lock(self.db):
                    await execute_with_retry(self.db, sql, params)
                    if hasattr(self.db, "commit"):
                        await commit_with_retry(self.db)

            logger.info(
                "meta_trade_resolved_logged",
                position_id=position_id,
                realised_pnl=realised_pnl,
                trade_won=trade_won
            )

        except Exception as e:
            logger.warning("meta_logger_resolve_error", error=str(e))

    async def _compute_sequential_state(
        self, profile_name: str, symbol: str, window_minutes: int
    ) -> Tuple[int, int, str]:
        """
        Query last 5 resolved trades for this (profile_name, symbol, window_minutes) ordered by closed_at DESC.
        Returns: (seq_consecutive_wins, seq_consecutive_losses, seq_last_5_outcomes_string)
        """
        sql = """
            SELECT realised_pnl 
            FROM profile_positions 
            WHERE profile_name = ? AND symbol = ? AND window_minutes = ? 
              AND closed_at IS NOT NULL AND realised_pnl IS NOT NULL
            ORDER BY closed_at DESC 
            LIMIT 5
        """
        try:
            rows = await fetch_all(self.db, sql, (profile_name, symbol, window_minutes))
            outcomes = []
            for r in rows:
                pnl = r["realised_pnl"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
                outcomes.append("W" if (pnl or 0.0) > 0.0 else "L")

            seq_outcomes_str = "".join(outcomes)

            seq_wins = 0
            seq_losses = 0
            if outcomes:
                first_outcome = outcomes[0]
                if first_outcome == "W":
                    for o in outcomes:
                        if o == "W":
                            seq_wins += 1
                        else:
                            break
                else:  # first_outcome == "L"
                    for o in outcomes:
                        if o == "L":
                            seq_losses += 1
                        else:
                            break

            return seq_wins, seq_losses, seq_outcomes_str

        except Exception as e:
            logger.warning("meta_logger_seq_state_failed", error=str(e))
            return 0, 0, ""

    async def _compute_trailing_stats(
        self, profile_name: str, opened_at: int, days: int
    ) -> Tuple[int, int, float, float]:
        """
        Query rolling window stats for a profile over the last N days relative to opened_at.
        Returns: (total_trades, win_count, win_rate, avg_entry_price)
        """
        cutoff = opened_at - (days * 86400)
        sql = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN realised_pnl > 0 THEN 1 ELSE 0 END) as wins,
                AVG(entry_price) as avg_entry
            FROM profile_positions 
            WHERE profile_name = ? AND closed_at IS NOT NULL AND realised_pnl IS NOT NULL
              AND closed_at >= ? AND closed_at <= ?
        """
        try:
            row = await fetch_one(self.db, sql, (profile_name, cutoff, opened_at))
            if not row:
                return 0, 0, 0.5, 0.0

            total = row["total"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            wins = row["wins"] if isinstance(row, dict) or hasattr(row, "keys") else row[1]
            avg_entry = row["avg_entry"] if isinstance(row, dict) or hasattr(row, "keys") else row[2]

            total = total or 0
            wins = wins or 0
            avg_entry = float(avg_entry or 0.0)

            win_rate = (wins / total) if total > 0 else 0.5
            return total, wins, win_rate, avg_entry

        except Exception as e:
            logger.warning("meta_logger_trailing_stats_failed", profile=profile_name, days=days, error=str(e))
            return 0, 0, 0.5, 0.0
