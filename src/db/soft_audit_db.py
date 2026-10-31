"""
Dedicated SQLite database helper for tracking soft-guards (B3 Ceilings, Falling Knife, Z-Score spikes)
specifically for 5-minute micro-market signals.
"""

import aiosqlite
import os
import time
from typing import Optional, Dict, Any, List
from src.logs.log_config import get_logger

logger = get_logger("soft_audit_db")

async def init_soft_audit_db(db_path: str) -> None:
    """Initialize the soft trades database schema."""
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        
    async with aiosqlite.connect(db_path, timeout=60.0) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 60000")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS soft_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                window_size TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                yes_price REAL NOT NULL,
                price_velocity REAL NOT NULL,
                z_score REAL NOT NULL,
                reasons TEXT NOT NULL,
                status TEXT NOT NULL,
                outcome TEXT DEFAULT NULL,
                pnl REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );
        """)
        await db.commit()
    logger.info("soft_audit_db_initialized", path=db_path)

async def log_soft_trade(
    db_path: str,
    market_id: str,
    symbol: str,
    window_size: str,
    side: str,
    entry_price: float,
    yes_price: float,
    price_velocity: float,
    z_score: float,
    reasons: str,
    status: str
) -> None:
    """Insert a new soft trade signal log into the audit database."""
    try:
        await init_soft_audit_db(db_path)
        async with aiosqlite.connect(db_path, timeout=60.0) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            timestamp = time.strftime('%Y-%m-%dT%H:%M:%fZ', time.gmtime())
            await db.execute("""
                INSERT INTO soft_trades (
                    market_id, symbol, window_size, side, entry_price,
                    yes_price, price_velocity, z_score, reasons, status, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id, symbol, str(window_size), side, entry_price,
                yes_price, price_velocity, z_score, reasons, status, timestamp
            ))
            await db.commit()
        logger.info(
            "soft_trade_logged",
            market_id=market_id,
            reasons=reasons,
            side=side,
            entry_price=entry_price
        )
    except Exception as e:
        logger.error("failed_to_log_soft_trade", error=str(e), market_id=market_id)

async def get_pending_soft_trades(db_path: str) -> List[Dict[str, Any]]:
    """Retrieve all soft trades that have not been resolved yet."""
    trades = []
    try:
        if not os.path.exists(db_path):
            return []
        async with aiosqlite.connect(db_path, timeout=60.0) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM soft_trades WHERE outcome IS NULL AND status = 'EXECUTED'"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    trades.append(dict(row))
    except Exception as e:
        logger.error("failed_to_get_pending_soft_trades", error=str(e))
    return trades

async def update_soft_trade_resolution(
    db_path: str,
    trade_id: int,
    outcome: str,
    pnl: float
) -> None:
    """Update a soft trade with its final resolution outcome and calculated PnL."""
    try:
        async with aiosqlite.connect(db_path, timeout=60.0) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            await db.execute(
                "UPDATE soft_trades SET outcome = ?, pnl = ?, status = 'RESOLVED' WHERE id = ?",
                (outcome, pnl, trade_id)
            )
            await db.commit()
        logger.info(
            "soft_trade_resolved",
            trade_id=trade_id,
            outcome=outcome,
            pnl=pnl
        )
    except Exception as e:
        logger.error("failed_to_update_soft_trade_resolution", error=str(e), trade_id=trade_id)
