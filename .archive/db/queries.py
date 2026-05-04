"""
Database query module for Polymarket Reverse Bot.
All SQL queries are parameterized — NEVER string-format SQL.
"""

import time
import uuid
import json
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiosqlite


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------

async def init_db(db_path: str) -> aiosqlite.Connection:
    """
    Initialize database: create file, apply schema, run migrations.
    Returns an open connection.
    """
    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row

    # Enable WAL mode for better concurrent read performance
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")

    # Apply schema
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()
    await db.executescript(schema_sql)

    # Apply migrations
    migrations_dir = Path(__file__).parent / "migrations"
    if migrations_dir.exists():
        migration_path = migrations_dir / "v0001_initial.sql"
        if migration_path.exists():
            migration_sql = migration_path.read_text()
            await db.executescript(migration_sql)

    await db.commit()
    return db


async def close_db(db: aiosqlite.Connection) -> None:
    """Close the database connection safely."""
    try:
        await db.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

async def upsert_market(db: aiosqlite.Connection, market: Dict[str, Any]) -> None:
    """Insert or update a market record."""
    await db.execute(
        """
        INSERT INTO markets (
            market_id, question, category, yes_price, no_price,
            yes_volume_24h, total_volume, resolution_source,
            closes_at, last_scanned_at, is_flagged, flag_reason,
            last_seen_above_threshold, consecutive_cycles_above
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            question = excluded.question,
            category = excluded.category,
            yes_price = excluded.yes_price,
            no_price = excluded.no_price,
            yes_volume_24h = excluded.yes_volume_24h,
            total_volume = excluded.total_volume,
            resolution_source = excluded.resolution_source,
            closes_at = excluded.closes_at,
            last_scanned_at = excluded.last_scanned_at,
            is_flagged = excluded.is_flagged,
            flag_reason = excluded.flag_reason,
            last_seen_above_threshold = excluded.last_seen_above_threshold,
            consecutive_cycles_above = excluded.consecutive_cycles_above
        """,
        (
            market["market_id"],
            market.get("question", ""),
            market.get("category"),
            market.get("yes_price"),
            market.get("no_price"),
            market.get("yes_volume_24h"),
            market.get("total_volume"),
            market.get("resolution_source"),
            market.get("closes_at"),
            market.get("last_scanned_at", int(time.time())),
            market.get("is_flagged", 0),
            market.get("flag_reason"),
            market.get("last_seen_above_threshold"),
            market.get("consecutive_cycles_above", 0),
        ),
    )
    await db.commit()


async def get_market(db: aiosqlite.Connection, market_id: str) -> Optional[Dict]:
    """Fetch a single market by ID."""
    cursor = await db.execute(
        "SELECT * FROM markets WHERE market_id = ?", (market_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_flagged_markets(db: aiosqlite.Connection) -> List[Dict]:
    """Get all flagged markets (candidates for trading)."""
    cursor = await db.execute(
        "SELECT * FROM markets WHERE is_flagged = 1 ORDER BY last_scanned_at DESC"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_market_threshold_tracking(
    db: aiosqlite.Connection,
    market_id: str,
    last_seen_above: int,
    consecutive_cycles: int,
) -> None:
    """Update the 2-cycle confirmation tracking fields."""
    await db.execute(
        """
        UPDATE markets
        SET last_seen_above_threshold = ?, consecutive_cycles_above = ?
        WHERE market_id = ?
        """,
        (last_seen_above, consecutive_cycles, market_id),
    )
    await db.commit()


async def reset_market_threshold_tracking(
    db: aiosqlite.Connection, market_id: str
) -> None:
    """Reset threshold tracking when price drops below threshold."""
    await db.execute(
        """
        UPDATE markets
        SET last_seen_above_threshold = NULL, consecutive_cycles_above = 0
        WHERE market_id = ?
        """,
        (market_id,),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

async def create_position(
    db: aiosqlite.Connection,
    market_id: str,
    entry_price: float,
    size_usdc: float,
    shares_bought: float,
    bot_version: str,
    paper: bool = False,
    notes: Optional[str] = None,
) -> str:
    """
    Create a new position with status PENDING.
    Returns the position_id (uuid4).
    """
    position_id = str(uuid.uuid4())
    table = "paper_positions" if paper else "positions"

    await db.execute(
        f"""
        INSERT INTO {table} (
            position_id, market_id, side, entry_price, size_usdc,
            shares_bought, order_status, opened_at, version_opened, notes
        ) VALUES (?, ?, 'NO', ?, ?, ?, 'PENDING', ?, ?, ?)
        """,
        (
            position_id,
            market_id,
            entry_price,
            size_usdc,
            shares_bought,
            int(time.time()),
            bot_version,
            notes,
        ),
    )
    await db.commit()
    return position_id


async def update_position_status(
    db: aiosqlite.Connection,
    position_id: str,
    order_status: str,
    order_id: Optional[str] = None,
    paper: bool = False,
) -> None:
    """Update position status and optionally the order_id."""
    table = "paper_positions" if paper else "positions"

    if order_id:
        await db.execute(
            f"UPDATE {table} SET order_status = ?, order_id = ? WHERE position_id = ?",
            (order_status, order_id, position_id),
        )
    else:
        await db.execute(
            f"UPDATE {table} SET order_status = ? WHERE position_id = ?",
            (order_status, position_id),
        )
    await db.commit()


async def close_position(
    db: aiosqlite.Connection,
    position_id: str,
    exit_price: float,
    realised_pnl: float,
    close_reason: str,
    paper: bool = False,
) -> None:
    """Close a position with exit data."""
    table = "paper_positions" if paper else "positions"

    await db.execute(
        f"""
        UPDATE {table}
        SET order_status = 'FILLED',
            closed_at = ?,
            exit_price = ?,
            realised_pnl = ?,
            close_reason = ?
        WHERE position_id = ?
        """,
        (int(time.time()), exit_price, realised_pnl, close_reason, position_id),
    )
    await db.commit()


async def get_open_positions(
    db: aiosqlite.Connection, paper: bool = False
) -> List[Dict]:
    """Get all currently open or pending positions."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT * FROM {table} WHERE order_status IN ('PENDING', 'OPEN', 'PARTIAL')"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def has_traded_market(
    db: aiosqlite.Connection, market_id: str, paper: bool = False
) -> bool:
    """Check if the bot has ever opened a position for this market."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT 1 FROM {table} WHERE market_id = ?", (market_id,)
    )
    row = await cursor.fetchone()
    return bool(row)



async def get_position(
    db: aiosqlite.Connection, position_id: str, paper: bool = False
) -> Optional[Dict]:
    """Fetch a single position by ID."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT * FROM {table} WHERE position_id = ?", (position_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def has_open_position_for_market(
    db: aiosqlite.Connection, market_id: str, paper: bool = False
) -> bool:
    """Check if there's already an OPEN or PENDING position for this market."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT 1 FROM {table} WHERE market_id = ? AND order_status IN ('OPEN', 'PENDING') LIMIT 1",
        (market_id,),
    )
    row = await cursor.fetchone()
    return row is not None


async def count_open_positions(
    db: aiosqlite.Connection, paper: bool = False
) -> int:
    """Count currently open or pending positions."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE order_status IN ('OPEN', 'PENDING')"
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_total_exposure(
    db: aiosqlite.Connection, paper: bool = False
) -> float:
    """Sum of size_usdc for all OPEN or PENDING positions."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(size_usdc), 0.0) FROM {table} WHERE order_status IN ('OPEN', 'PENDING')"
    )
    row = await cursor.fetchone()
    return float(row[0]) if row else 0.0


async def get_daily_realised_loss(
    db: aiosqlite.Connection, paper: bool = False
) -> float:
    """
    Sum of negative realised_pnl for positions closed today (UTC).
    Returns a positive number representing total losses.
    """
    table = "paper_positions" if paper else "positions"
    # Start of today UTC in unix timestamp
    import calendar
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start_of_day = int(calendar.timegm(now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timetuple()))

    cursor = await db.execute(
        f"""
        SELECT COALESCE(SUM(ABS(realised_pnl)), 0.0)
        FROM {table}
        WHERE closed_at >= ? AND realised_pnl < 0
        """,
        (start_of_day,),
    )
    row = await cursor.fetchone()
    return float(row[0]) if row else 0.0


# ---------------------------------------------------------------------------
# Scan Log
# ---------------------------------------------------------------------------

async def log_scan(
    db: aiosqlite.Connection,
    markets_scanned: int,
    markets_flagged: int,
    signals_fired: int,
    orders_placed: int,
    bot_version: str,
    errors: Optional[str] = None,
) -> int:
    """Record a scan cycle. Returns the scan_id."""
    cursor = await db.execute(
        """
        INSERT INTO scan_log (
            scanned_at, markets_scanned, markets_flagged,
            signals_fired, orders_placed, errors, bot_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time()),
            markets_scanned,
            markets_flagged,
            signals_fired,
            orders_placed,
            errors,
            bot_version,
        ),
    )
    await db.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Events Log (append-only — NEVER update or delete)
# ---------------------------------------------------------------------------

async def log_event(
    db: aiosqlite.Connection,
    event_type: str,
    bot_version: str,
    market_id: Optional[str] = None,
    position_id: Optional[str] = None,
    payload: Optional[Dict] = None,
) -> int:
    """Append an event to the audit log. Returns the event_id."""
    cursor = await db.execute(
        """
        INSERT INTO events_log (
            event_time, event_type, market_id, position_id, payload, bot_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time()),
            event_type,
            market_id,
            position_id,
            json.dumps(payload) if payload else None,
            bot_version,
        ),
    )
    await db.commit()
    return cursor.lastrowid
# ---------------------------------------------------------------------------
# Performance Reporting
# ---------------------------------------------------------------------------

async def get_performance_summary(db: aiosqlite.Connection, paper: bool = False) -> Dict[str, Any]:
    """
    Calculate high-level performance metrics from the positions table.
    """
    table = "paper_positions" if paper else "positions"
    
    # Total trades (anything that reached FILLED/CLOSED status)
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED'"
    )
    row = await cursor.fetchone()
    total_closed = row[0] if row else 0
    
    # Wins (realised_pnl > 0)
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl > 0"
    )
    row = await cursor.fetchone()
    wins = row[0] if row else 0
    
    # Losses (realised_pnl < 0)
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl < 0"
    )
    row = await cursor.fetchone()
    losses = row[0] if row else 0
    
    # Total Realised P&L
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE order_status = 'FILLED'"
    )
    row = await cursor.fetchone()
    total_pnl = float(row[0]) if row else 0.0
    
    # Current Open Exposure
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(size_usdc), 0.0), COUNT(*) FROM {table} WHERE order_status = 'OPEN'"
    )
    row = await cursor.fetchone()
    open_exposure = float(row[0]) if row else 0.0
    open_count = row[1] if row else 0
    
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0
    
    return {
        "total_closed": total_closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "total_realised_pnl": round(total_pnl, 4),
        "open_exposure": round(open_exposure, 2),
        "open_count": open_count,
        "is_paper": paper
    }
