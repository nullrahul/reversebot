import os
import time
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from src.logs.log_config import get_logger
from src.db.queries import get_db_lock, execute_with_retry, commit_with_retry, fetch_all, fetch_one

logger = get_logger("guardian")


class Guardian:
    STREAK_LIMITS = {
        "main_paper": 14,
        "stop_loss": 28,
        "reversal_stop_loss": 21,
        "stop_loss_filtered": 28,
        "reversal_stop_loss_filtered": 21
    }
    DEFAULT_STREAK_FALLBACK = 10

    def __init__(self, db, position_mgr):
        self.db = db
        self.position_mgr = position_mgr

    async def init_tables(self) -> None:
        """Initialize guardian state table in the database."""
        try:
            async with get_db_lock(self.db):
                await execute_with_retry(self.db, """
                    CREATE TABLE IF NOT EXISTS profile_states (
                        profile_name TEXT PRIMARY KEY,
                        state TEXT NOT NULL,                  -- 'ACTIVE', 'PAUSED', 'DISABLED'
                        current_streak INTEGER DEFAULT 0,
                        daily_drawdown REAL DEFAULT 0.0,
                        paused_at INTEGER,
                        disabled_at INTEGER,
                        last_evaluated INTEGER
                    )
                """)
                await commit_with_retry(self.db)
                
                # Alter table to add rearmed_at column if it does not exist
                try:
                    await execute_with_retry(self.db, "ALTER TABLE profile_states ADD COLUMN rearmed_at INTEGER DEFAULT 0")
                    await commit_with_retry(self.db)
                except sqlite3.OperationalError as oe:
                    if "duplicate column name" not in str(oe).lower():
                        raise
                    
            logger.info("guardian_tables_initialized")
        except Exception as e:
            logger.error("failed_to_initialize_guardian_tables", error=str(e))

    def evaluate_time_regime(self, profile_name: str) -> bool:
        """
        Evaluate UTC time-based operational windows:
        - For 'reversal_stop_loss_filtered': Return PAUSED (False) if current UTC hour is between 08:00 and 12:00.
        - For 'stop_loss_filtered': Return PAUSED (False) if current UTC hour is between 20:00 and 02:00.
        """
        utc_now = datetime.now(timezone.utc)
        hour = utc_now.hour

        if profile_name == "reversal_stop_loss_filtered":
            # UTC 08:00 - 12:00 (8 <= hour < 12)
            if 8 <= hour < 12:
                logger.info("guardian_time_regime_pause_reversal", hour=hour)
                return False

        elif profile_name == "stop_loss_filtered":
            # UTC 20:00 - 02:00 (hour >= 20 or hour < 2)
            if hour >= 20 or hour < 2:
                logger.info("guardian_time_regime_pause_stop_loss", hour=hour)
                return False

        return True

    def get_scaled_size(self, profile_name: str, base_size: float) -> float:
        """
        Weekend Modifier: If UTC day is Friday, Saturday, or Sunday, scale the position size:
        - 'reversal_stop_loss_filtered': 1.25x
        - 'stop_loss_filtered': 0.75x
        """
        utc_now = datetime.now(timezone.utc)
        weekday = utc_now.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

        if weekday in (4, 5, 6):  # Friday, Saturday, Sunday
            if profile_name == "reversal_stop_loss_filtered":
                scaled = base_size * 1.25
                logger.info("guardian_weekend_size_scaled_reversal", original=base_size, scaled=scaled)
                return scaled
            elif profile_name == "stop_loss_filtered":
                scaled = base_size * 0.75
                logger.info("guardian_weekend_size_scaled_stop_loss", original=base_size, scaled=scaled)
                return scaled

        return base_size

    async def is_profile_allowed(self, profile_name: str) -> bool:
        """
        Evaluate all rules: daily drawdown, losing streak, and time-based regime filters.
        Returns True if profile is ACTIVE, False if PAUSED or DISABLED.
        """
        # 1. First evaluate the time regime (soft pause)
        if not self.evaluate_time_regime(profile_name):
            return False

        # 2. Query/evaluate database states
        try:
            # Enforce database status
            row = await fetch_one(self.db, "SELECT state, rearmed_at FROM profile_states WHERE profile_name = ?", (profile_name,))
            if not row:
                now_ts = int(time.time())
                async with get_db_lock(self.db):
                    await execute_with_retry(self.db, "INSERT OR IGNORE INTO profile_states (profile_name, state, rearmed_at) VALUES (?, 'ACTIVE', ?)", (profile_name, now_ts))
                    await commit_with_retry(self.db)
                current_state = "ACTIVE"
                rearmed_at = now_ts
            else:
                current_state = row["state"]
                rearmed_at = row["rearmed_at"] if ("rearmed_at" in row.keys() and row["rearmed_at"] is not None) else 0

            # If disabled or paused, immediately reject (no shadow evaluation)
            if current_state in ("PAUSED", "DISABLED"):
                return False

            # 3. Evaluate streak and drawdown rules
            await self._evaluate_db_rules(profile_name, current_state, rearmed_at)

            # Re-read state after evaluation
            row = await fetch_one(self.db, "SELECT state FROM profile_states WHERE profile_name = ?", (profile_name,))
            if row and row["state"] in ("PAUSED", "DISABLED"):
                return False

            return True
        except Exception as err:
            logger.error("guardian_allowance_evaluation_error", profile_name=profile_name, error=str(err))
            return True # Fallback to true on db errors to prevent lockout

    async def _evaluate_db_rules(self, profile_name: str, current_state: str, rearmed_at: int) -> None:
        """Evaluate streak breaker and daily drawdown in the DB."""
        now_ts = int(time.time())
        table = "paper_positions" if profile_name == "main_paper" else "profile_positions"
        prof_filter = "1=1" if profile_name == "main_paper" else f"profile_name = '{profile_name}'"

        # Query recent trades since rearmed_at
        rows = await fetch_all(self.db, f"""
            SELECT realised_pnl FROM {table}
            WHERE {prof_filter} AND order_status = 'FILLED' AND realised_pnl IS NOT NULL
              AND opened_at > ?
            ORDER BY opened_at DESC
        """, (rearmed_at,))

        # Calculate active consecutive losses
        active_streak = 0
        for r in rows:
            pnl = r["realised_pnl"]
            if pnl < 0:
                active_streak += 1
            else:
                break

        limit = self.STREAK_LIMITS.get(profile_name, self.DEFAULT_STREAK_FALLBACK)

        # Transition ACTIVE -> PAUSED on streak breach
        if current_state == "ACTIVE" and active_streak >= limit:
            async with get_db_lock(self.db):
                await execute_with_retry(self.db, """
                    UPDATE profile_states 
                    SET state = 'PAUSED', paused_at = ?, current_streak = ?, last_evaluated = ?
                    WHERE profile_name = ?
                """, (now_ts, active_streak, now_ts, profile_name))
                await commit_with_retry(self.db)
            logger.warning("guardian_streak_breach_paused", profile_name=profile_name, streak=active_streak, limit=limit)
            current_state = "PAUSED"

        # UTC day drawdown starting after max(start_of_utc_day, rearmed_at)
        start_of_utc_day = int(time.time() - (time.time() % 86400))
        drawdown_start = max(start_of_utc_day, rearmed_at)
        daily_pnl_row = await fetch_one(self.db, f"""
            SELECT SUM(realised_pnl) FROM {table}
            WHERE {prof_filter} AND order_status = 'FILLED' AND realised_pnl IS NOT NULL
              AND opened_at >= ?
        """, (drawdown_start,))
        daily_pnl = daily_pnl_row[0] if daily_pnl_row and daily_pnl_row[0] is not None else 0.0

        # Drawdown limit (5% of default $1000 bankroll = $50)
        drawdown_limit = 50.0
        if daily_pnl < 0 and abs(daily_pnl) > drawdown_limit and current_state != "DISABLED":
            async with get_db_lock(self.db):
                await execute_with_retry(self.db, """
                    UPDATE profile_states 
                    SET state = 'DISABLED', disabled_at = ?, daily_drawdown = ?, last_evaluated = ?
                    WHERE profile_name = ?
                """, (now_ts, daily_pnl, now_ts, profile_name))
                await commit_with_retry(self.db)
            logger.error("guardian_drawdown_killswitch_disabled", profile_name=profile_name, daily_pnl=daily_pnl, limit=drawdown_limit)

    def re_arm(self, profile_name: str) -> None:
        """Manual override to re-arm a DISABLED or PAUSED profile."""
        # Note: This executes synchronously as it's typically triggered via CLI/dashboard
        conn = sqlite3.connect(os.path.expanduser("~/reversebot/data/reversebot_rd.db"), timeout=60.0)
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
            cursor = conn.cursor()
            
            # Alter table to add rearmed_at column if it does not exist (sync connection)
            try:
                cursor.execute("ALTER TABLE profile_states ADD COLUMN rearmed_at INTEGER DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError as oe:
                if "duplicate column name" not in str(oe).lower():
                    pass
                    
            now_ts = int(time.time())
            cursor.execute("""
                UPDATE profile_states 
                SET state = 'ACTIVE', disabled_at = NULL, paused_at = NULL, rearmed_at = ?, daily_drawdown = 0.0, current_streak = 0, last_evaluated = ?
                WHERE profile_name = ?
            """, (now_ts, now_ts, profile_name))
            conn.commit()
        finally:
            conn.close()
        logger.info("guardian_profile_manually_rearmed", profile_name=profile_name)
