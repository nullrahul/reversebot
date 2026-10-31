"""
DB Writer — Batched SQLite writer with daily rotation and WAL mode.
"""

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from src.logs.log_config import get_logger
from src.recorder.config import TICKS_DB_DIR

logger = get_logger("tick_db_writer")

class TickDBWriter:
    def __init__(self, db_dir: str = TICKS_DB_DIR):
        self.db_dir = db_dir
        os.makedirs(self.db_dir, exist_ok=True)
        self.current_day: Optional[str] = None
        self.conn: Optional[sqlite3.Connection] = None
        self._schema_sql = self._load_schema_sql()

    def _load_schema_sql(self) -> str:
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r") as f:
                return f.read()
        return ""

    def _get_today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def get_db_path(self, day_str: Optional[str] = None) -> str:
        d = day_str or self._get_today_str()
        return os.path.join(self.db_dir, f"ticks_{d}.db")

    def _ensure_connection(self):
        today = self._get_today_str()
        if self.conn is None or self.current_day != today:
            if self.conn:
                try:
                    self.conn.commit()
                    self.conn.close()
                except Exception:
                    pass

            self.current_day = today
            db_path = self.get_db_path(today)
            self.conn = sqlite3.connect(db_path, timeout=30.0)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            
            if self._schema_sql:
                self.conn.executescript(self._schema_sql)
            self.conn.commit()
            logger.info("tick_db_rotated", db_path=db_path, day=today)

    def write_batch(self, snapshots: List[Dict[str, Any]], external_ticks: List[Dict[str, Any]]):
        """Insert buffered snapshots and external ticks into SQLite transactionally."""
        if not snapshots and not external_ticks:
            return

        try:
            self._ensure_connection()
            cursor = self.conn.cursor()

            # Insert market snapshots
            if snapshots:
                snap_rows = []
                for s in snapshots:
                    snap_rows.append((
                        s.get("timestamp_ms"),
                        s.get("market_id"),
                        s.get("symbol"),
                        s.get("window_minutes"),
                        s.get("closes_at"),
                        s.get("yes_price"),
                        s.get("no_price"),
                        s.get("yes_best_bid"),
                        s.get("yes_best_ask"),
                        s.get("no_best_bid"),
                        s.get("no_best_ask"),
                        s.get("bid_ask_spread"),
                        s.get("book_imbalance"),
                        s.get("depth_usd_1c"),
                        s.get("depth_usd_5c"),
                        s.get("ask_depth_usd_1c"),
                        s.get("bid_depth_usd_1c"),
                        s.get("cvd_60s"),
                        s.get("cvd_5m"),
                        s.get("streak_depth"),
                        s.get("streak_quality"),
                        s.get("persistence_score"),
                        s.get("strategy_mode"),
                        s.get("spot_price"),
                        s.get("spot_velocity_10s"),
                        0.0,  # spot_velocity_z
                        0.0,  # distance_bps
                        s.get("realized_vol_5m"),
                        0.0,  # realized_vol_1h
                        s.get("seconds_remaining"),
                        0.0,  # market_age_pct
                        0.0,  # bs_implied_prob
                        0.0   # ev_at_snapshot
                    ))

                cursor.executemany("""
                    INSERT INTO market_snapshots (
                        timestamp_ms, market_id, symbol, window_minutes, closes_at,
                        yes_price, no_price, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask,
                        bid_ask_spread, book_imbalance, depth_usd_1c, depth_usd_5c,
                        ask_depth_usd_1c, bid_depth_usd_1c, cvd_60s, cvd_5m,
                        streak_depth, streak_quality, persistence_score, strategy_mode,
                        spot_price, spot_velocity_10s, spot_velocity_z, distance_bps,
                        realized_vol_5m, realized_vol_1h, seconds_remaining, market_age_pct,
                        bs_implied_prob, ev_at_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, snap_rows)

            # Insert external ticks
            if external_ticks:
                ext_rows = []
                for e in external_ticks:
                    ext_rows.append((
                        e.get("timestamp_ms"),
                        e.get("symbol"),
                        e.get("spot_price"),
                        e.get("cvd_60s"),
                        e.get("cvd_5m"),
                        e.get("spot_velocity_10s"),
                        0.0,  # spot_velocity_z
                        e.get("realized_vol_5m"),
                        e.get("realized_vol_1h")
                    ))

                cursor.executemany("""
                    INSERT INTO external_ticks (
                        timestamp_ms, symbol, spot_price, cvd_60s, cvd_5m,
                        spot_velocity_10s, spot_velocity_z, realized_vol_5m, realized_vol_1h
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, ext_rows)

            self.conn.commit()
            logger.info("tick_batch_written", snapshot_count=len(snapshots), ext_count=len(external_ticks))

        except Exception as e:
            logger.error("tick_batch_write_failed", error=str(e))
            if self.conn:
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    def log_gap(self, gap_start_ms: int, gap_end_ms: int, reason: str):
        """Record a data gap event into database."""
        try:
            self._ensure_connection()
            self.conn.execute("""
                INSERT INTO data_gaps (gap_start_ms, gap_end_ms, reason)
                VALUES (?, ?, ?)
            """, (gap_start_ms, gap_end_ms, reason))
            self.conn.commit()
        except Exception as e:
            logger.error("log_gap_failed", error=str(e))

    def close(self):
        if self.conn:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass
            self.conn = None
