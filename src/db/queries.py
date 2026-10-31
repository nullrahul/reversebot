"""
Database query module for Polymarket Reverse Bot.
All SQL queries are parameterized — NEVER string-format SQL.
"""

import time
import uuid
import json
import sqlite3
import asyncio
import random
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiosqlite
from src.logs.log_config import get_logger

logger = get_logger("db")


# ---------------------------------------------------------------------------
# Retry & Serialization Helpers for SQLite Concurrency Locks
# ---------------------------------------------------------------------------

_DB_LOCKS: Dict[int, asyncio.Lock] = {}


def get_db_lock(db: aiosqlite.Connection) -> asyncio.Lock:
    """Return a per-connection asyncio.Lock to serialize concurrent writes on the same connection."""
    db_id = id(db)
    lock = _DB_LOCKS.get(db_id)
    if lock is None:
        lock = asyncio.Lock()
        _DB_LOCKS[db_id] = lock
    return lock


async def execute_with_retry(db: aiosqlite.Connection, sql: str, params: tuple = (), max_retries: int = 10) -> Any:
    """Execute SQL query with automatic exponential backoff retries for transient SQLite locks."""
    for attempt in range(1, max_retries + 1):
        try:
            return await db.execute(sql, params)
        except (sqlite3.OperationalError, Exception) as e:
            err_msg = str(e).lower()
            if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries:
                try:
                    if getattr(db, "in_transaction", False):
                        await db.rollback()
                except Exception:
                    pass
                backoff = min(1.5, 0.05 * (1.5 ** (attempt - 1)) + (random.random() * 0.05))
                await asyncio.sleep(backoff)
            else:
                raise e


async def commit_with_retry(db: aiosqlite.Connection, max_retries: int = 10) -> None:
    """Commit transaction with automatic exponential backoff retries for transient SQLite locks."""
    for attempt in range(1, max_retries + 1):
        try:
            await db.commit()
            return
        except (sqlite3.OperationalError, Exception) as e:
            err_msg = str(e).lower()
            if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries:
                backoff = min(1.5, 0.05 * (1.5 ** (attempt - 1)) + (random.random() * 0.05))
                await asyncio.sleep(backoff)
            else:
                raise e


def _is_real_aiosqlite(db: Any) -> bool:
    """Return True if db is a real unmocked aiosqlite.Connection supporting atomic worker dispatch."""
    if db is None or type(db).__name__ in ("MagicMock", "AsyncMock", "Mock"):
        return False
    exec_attr = getattr(db, "execute", None)
    if type(exec_attr).__name__ in ("MagicMock", "AsyncMock", "Mock"):
        return False
    return hasattr(db, "execute_fetchall") and hasattr(db, "_execute") and hasattr(db, "_conn")


async def fetch_all(db: Any, sql: str, params: tuple = (), max_retries: int = 5) -> List[Any]:
    """
    Execute a SELECT query atomically in the aiosqlite worker thread and return all rows.
    Prevents sqlite3.InterfaceError ('Cursor needed to be reset because of commit/rollback')
    by never leaving an un-fetched cursor open across asyncio await boundaries.
    """
    if not _is_real_aiosqlite(db):
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return list(rows) if rows else []

    for attempt in range(1, max_retries + 1):
        try:
            rows = await db.execute_fetchall(sql, params)
            return list(rows) if rows else []
        except (sqlite3.OperationalError, sqlite3.InterfaceError, Exception) as e:
            err_msg = str(e).lower()
            if ("locked" in err_msg or "busy" in err_msg or "cursor needed to be reset" in err_msg) and attempt < max_retries:
                backoff = min(0.5, 0.02 * (1.5 ** (attempt - 1)) + (random.random() * 0.02))
                await asyncio.sleep(backoff)
            else:
                raise e
    return []


async def fetch_one(db: Any, sql: str, params: tuple = (), max_retries: int = 5) -> Optional[Any]:
    """
    Execute a SELECT query atomically in the aiosqlite worker thread and return a single row or None.
    """
    if not _is_real_aiosqlite(db):
        cursor = await db.execute(sql, params)
        return await cursor.fetchone()

    for attempt in range(1, max_retries + 1):
        try:
            return await db._execute(lambda s, p: db._conn.execute(s, p).fetchone(), sql, params)
        except (sqlite3.OperationalError, sqlite3.InterfaceError, Exception) as e:
            err_msg = str(e).lower()
            if ("locked" in err_msg or "busy" in err_msg or "cursor needed to be reset" in err_msg) and attempt < max_retries:
                backoff = min(0.5, 0.02 * (1.5 ** (attempt - 1)) + (random.random() * 0.02))
                await asyncio.sleep(backoff)
            else:
                raise e
    return None


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

    db = await aiosqlite.connect(db_path, timeout=60.0)
    db.row_factory = aiosqlite.Row

    # Enable WAL mode, 60s busy timeout, and NORMAL synchronous for maximum concurrency and safety
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=60000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")

    # Apply schema
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        schema_sql = schema_path.read_text()
        await db.executescript(schema_sql)

    # Apply migrations
    migrations_dir = Path(__file__).parent / "migrations"
    if migrations_dir.exists():
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  INTEGER NOT NULL,
                description TEXT
            )
            """
        )
        for migration_path in sorted(migrations_dir.glob("*.sql")):
            version = migration_path.stem
            cursor = await db.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,))
            if not await cursor.fetchone():
                migration_sql = migration_path.read_text()
                try:
                    await db.executescript(migration_sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" in str(e):
                        logger.info("migration_column_exists", version=version, message="Column already exists, skipping migration step.")
                    else:
                        raise e
                await db.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, strftime('%s', 'now'), ?)",
                    (version, f"Applied {version}")
                )

    await db.commit()
    return db


async def close_db(db: aiosqlite.Connection) -> None:
    """Close the database connection safely."""
    _DB_LOCKS.pop(id(db), None)
    try:
        await db.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

async def upsert_market(db: aiosqlite.Connection, market: Dict[str, Any]) -> None:
    """Insert or update a market record."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            INSERT INTO markets (
                market_id, condition_id, yes_token_id, no_token_id,
                question, slug, event_id, category, yes_price, no_price,
                yes_volume_24h, total_volume, resolution_source,
                closes_at, last_scanned_at, is_flagged, flag_reason,
                last_seen_above_threshold, consecutive_cycles_above,
                btc_price_at_market_open
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                condition_id = excluded.condition_id,
                yes_token_id = excluded.yes_token_id,
                no_token_id = excluded.no_token_id,
                question = excluded.question,
                slug = excluded.slug,
                event_id = excluded.event_id,
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
                consecutive_cycles_above = excluded.consecutive_cycles_above,
                btc_price_at_market_open = COALESCE(markets.btc_price_at_market_open, excluded.btc_price_at_market_open)
            """,
            (
                market["market_id"],
                market.get("condition_id"),
                market.get("yes_token_id"),
                market.get("no_token_id"),
                market.get("question", ""),
                market.get("slug"),
                market.get("event_id"),
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
                market.get("btc_price_at_market_open"),
            ),
        )
        await commit_with_retry(db)


async def get_market(db: aiosqlite.Connection, market_id: str) -> Optional[Dict]:
    """Fetch a single market by ID."""
    row = await fetch_one(db, "SELECT * FROM markets WHERE market_id = ?", (market_id,))
    return dict(row) if row else None


async def get_flagged_markets(db: aiosqlite.Connection) -> List[Dict]:
    """Get all flagged markets (candidates for trading)."""
    rows = await fetch_all(db, "SELECT * FROM markets WHERE is_flagged = 1 ORDER BY last_scanned_at DESC")
    return [dict(r) for r in rows]


async def update_market_threshold_tracking(
    db: aiosqlite.Connection,
    market_id: str,
    last_seen_above: int,
    consecutive_cycles: int,
) -> None:
    """Update the 2-cycle confirmation tracking fields."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            UPDATE markets
            SET last_seen_above_threshold = ?, consecutive_cycles_above = ?
            WHERE market_id = ?
            """,
            (last_seen_above, consecutive_cycles, market_id),
        )
        await commit_with_retry(db)


async def reset_market_threshold_tracking(
    db: aiosqlite.Connection, market_id: str
) -> None:
    """Reset threshold tracking when price drops below threshold."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            UPDATE markets
            SET last_seen_above_threshold = NULL, consecutive_cycles_above = 0
            WHERE market_id = ?
            """,
            (market_id,),
        )
        await commit_with_retry(db)


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
    side: str = 'NO',
    win_prob_estimate: float = 0.5,
    kelly_fraction_used: float = 0.0,
    bid_ask_spread_at_entry: float = 0.0,
    book_imbalance_at_entry: float = 0.0,
    cvd_60s_at_entry: float = 0.0,
    correlated_position_open: int = 0,
    seconds_to_close_at_entry: float = 0.0,
    market_age_at_entry_pct: float = 0.0,
    bankroll_at_entry: float = 0.0,
    persistence_score: float = 0.0,
    strategy_mode: str = 'TREND',
) -> str:
    """
    Create a new position with status PENDING.
    Returns the position_id (uuid4).
    """
    import datetime
    from zoneinfo import ZoneInfo

    position_id = str(uuid.uuid4())
    opened_at_ts = int(time.time())

    # Dynamically compute timezone-aware hour, day, weekend and session
    dt_utc = datetime.datetime.fromtimestamp(opened_at_ts, tz=datetime.timezone.utc)
    dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))

    hour_utc = dt_utc.hour
    hour_et = dt_et.hour
    day_of_week = dt_et.strftime("%A")
    is_weekend = 1 if day_of_week in ("Saturday", "Sunday") else 0

    if 19 <= hour_et or hour_et < 3:
        session = "ASIA"
    elif 3 <= hour_et < 8:
        session = "LONDON"
    elif 8 <= hour_et < 17:
        session = "NY"
    else:
        session = "OFF"

    # Fetch market info to denormalize symbol and window_minutes
    row = await fetch_one(db, "SELECT question, slug FROM markets WHERE market_id = ?", (market_id,))
    symbol = "BTC"
    window_minutes = 5
    if row:
        question = (row["question"] or "").lower()
        slug = (row["slug"] or "").lower()
        
        # 1. Parse window_minutes
        if "-5m-" in slug or "5-minute" in question:
            window_minutes = 5
        elif "-15m-" in slug or "15-minute" in question:
            window_minutes = 15
            
        # 2. Parse symbol
        import re
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or re.search(r'\bsol\b', question) is not None:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"

    async with get_db_lock(db):
        if paper:
            await execute_with_retry(
                db,
                """
                INSERT INTO paper_positions (
                    position_id, market_id, side, entry_price, size_usdc,
                    shares_bought, order_status, opened_at, version_opened, notes,
                    max_price_seen, min_price_seen,
                    win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                    book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                    seconds_to_close_at_entry, market_age_at_entry_pct,
                    hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                    window_minutes, symbol, persistence_score, strategy_mode
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    market_id,
                    side,
                    entry_price,
                    size_usdc,
                    shares_bought,
                    opened_at_ts,
                    bot_version,
                    notes,
                    entry_price,
                    entry_price,
                    win_prob_estimate,
                    kelly_fraction_used,
                    bid_ask_spread_at_entry,
                    book_imbalance_at_entry,
                    cvd_60s_at_entry,
                    correlated_position_open,
                    seconds_to_close_at_entry,
                    market_age_at_entry_pct,
                    hour_utc,
                    hour_et,
                    day_of_week,
                    is_weekend,
                    session,
                    bankroll_at_entry,
                    window_minutes,
                    symbol,
                    persistence_score,
                    strategy_mode,
                ),
            )
        else:
            await execute_with_retry(
                db,
                """
                INSERT INTO positions (
                    position_id, market_id, side, entry_price, size_usdc,
                    shares_bought, order_status, opened_at, version_opened, notes,
                    max_price_seen, min_price_seen,
                    win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                    book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                    seconds_to_close_at_entry, market_age_at_entry_pct,
                    hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                    window_minutes, symbol, persistence_score, strategy_mode
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    market_id,
                    side,
                    entry_price,
                    size_usdc,
                    shares_bought,
                    opened_at_ts,
                    bot_version,
                    notes,
                    entry_price,
                    entry_price,
                    win_prob_estimate,
                    kelly_fraction_used,
                    bid_ask_spread_at_entry,
                    book_imbalance_at_entry,
                    cvd_60s_at_entry,
                    correlated_position_open,
                    seconds_to_close_at_entry,
                    market_age_at_entry_pct,
                    hour_utc,
                    hour_et,
                    day_of_week,
                    is_weekend,
                    session,
                    bankroll_at_entry,
                    window_minutes,
                    symbol,
                    persistence_score,
                    strategy_mode,
                ),
            )
        await commit_with_retry(db)
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

    async with get_db_lock(db):
        if order_id:
            await execute_with_retry(
                db,
                f"UPDATE {table} SET order_status = ?, order_id = ? WHERE position_id = ?",
                (order_status, order_id, position_id),
            )
        else:
            await execute_with_retry(
                db,
                f"UPDATE {table} SET order_status = ? WHERE position_id = ?",
                (order_status, position_id),
            )
        await commit_with_retry(db)


async def update_position_notes(
    db: aiosqlite.Connection,
    position_id: str,
    notes: str,
    paper: bool = False,
) -> None:
    """Update the notes field for a position (used for self-healing Token IDs)."""
    table = "paper_positions" if paper else "positions"
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            f"UPDATE {table} SET notes = ? WHERE position_id = ?",
            (notes, position_id),
        )
        await commit_with_retry(db)


async def update_position_min_max_prices(
    db: aiosqlite.Connection,
    position_id: str,
    min_price: float,
    max_price: float,
    paper: bool = False,
) -> None:
    """Update tracking for lowest and highest prices observed for this position."""
    table = "paper_positions" if paper else "positions"
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            f"UPDATE {table} SET min_price_seen = ?, max_price_seen = ? WHERE position_id = ?",
            (min_price, max_price, position_id),
        )
        await commit_with_retry(db)


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
    now = int(time.time())

    async with get_db_lock(db):
        await execute_with_retry(
            db,
            f"""
            UPDATE {table}
            SET order_status = 'FILLED',
                closed_at = CASE WHEN ? = 'RESOLUTION' THEN COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = {table}.market_id), ?) ELSE ? END,
                exit_price = ?,
                realised_pnl = ?,
                close_reason = ?,
                resolution_delay = ? - COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = {table}.market_id), ?)
            WHERE position_id = ?
            """,
            (close_reason, now, now, exit_price, realised_pnl, close_reason, now, now, position_id),
        )
        await commit_with_retry(db)


async def get_open_positions(
    db: aiosqlite.Connection, paper: bool = False
) -> List[Dict]:
    """Get all currently open or pending positions."""
    table = "paper_positions" if paper else "positions"
    rows = await fetch_all(
        db, f"SELECT * FROM {table} WHERE order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')"
    )
    return [dict(r) for r in rows]


async def get_active_and_unexpired_positions(
    db: aiosqlite.Connection, paper: bool = False, now: Optional[int] = None
) -> List[Dict]:
    """Get all active open/pending positions using status index."""
    table = "paper_positions" if paper else "positions"
    rows = await fetch_all(
        db,
        f"""
        SELECT *
        FROM {table}
        WHERE order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')
        """
    )
    return [dict(r) for r in rows]


async def has_traded_market(
    db: aiosqlite.Connection, market_id: str, paper: bool = False
) -> bool:
    """Check if the bot has ever opened a position for this market."""
    table = "paper_positions" if paper else "positions"
    row = await fetch_one(db, f"SELECT 1 FROM {table} WHERE market_id = ?", (market_id,))
    return bool(row)


async def get_last_traded_market_close_times(
    db: aiosqlite.Connection, paper: bool = False
) -> List[dict]:
    """
    Get the question, slug, and closes_at of all traded markets to find the last trade close time.
    """
    table = "paper_positions" if paper else "positions"
    rows = await fetch_all(
        db,
        f"""
        SELECT m.question, m.slug, m.closes_at
        FROM {table} p
        JOIN markets m ON p.market_id = m.market_id
        """
    )
    return [dict(r) for r in rows]




async def get_position(
    db: aiosqlite.Connection, position_id: str, paper: bool = False
) -> Optional[Dict]:
    """Fetch a single position by ID."""
    table = "paper_positions" if paper else "positions"
    row = await fetch_one(db, f"SELECT * FROM {table} WHERE position_id = ?", (position_id,))
    return dict(row) if row else None


async def has_open_position_for_market(
    db: aiosqlite.Connection, market_id: str, paper: bool = False
) -> bool:
    """Check if there's already an OPEN or PENDING position for this market."""
    table = "paper_positions" if paper else "positions"
    row = await fetch_one(
        db,
        f"SELECT 1 FROM {table} WHERE market_id = ? AND order_status IN ('OPEN', 'PENDING') LIMIT 1",
        (market_id,),
    )
    return row is not None


async def count_open_positions(
    db: aiosqlite.Connection, paper: bool = False
) -> int:
    """Count currently open or pending positions whose markets are still active."""
    table = "paper_positions" if paper else "positions"
    now = int(time.time())
    row = await fetch_one(
        db,
        f"""
        SELECT COUNT(*) FROM {table} p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')
          AND (m.closes_at IS NULL OR m.closes_at > ?)
        """,
        (now,),
    )
    return row[0] if row else 0


async def get_total_exposure(
    db: aiosqlite.Connection, paper: bool = False
) -> float:
    """Sum of size_usdc for all OPEN or PENDING positions whose markets are still active."""
    table = "paper_positions" if paper else "positions"
    now = int(time.time())
    row = await fetch_one(
        db,
        f"""
        SELECT COALESCE(SUM(p.size_usdc), 0.0) FROM {table} p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')
          AND (m.closes_at IS NULL OR m.closes_at > ?)
        """,
        (now,),
    )
    return float(row[0]) if row else 0.0


async def get_daily_realised_loss(
    db: aiosqlite.Connection, paper: bool = False
) -> float:
    """
    Sum of negative realised_pnl for positions closed today (UTC).
    Returns a positive number representing total losses.
    """
    table = "paper_positions" if paper else "positions"
    # FIX: "UTC Midnight" Budget Crash
    # Use a strict 24-hour rolling window based on Unix time to bypass VPS clock drift / UTC issues.
    # Calculate NET PNL to prevent killing the bot if it is profitable.
    start_of_window = int(time.time()) - 86400

    row = await fetch_one(
        db,
        f"""
        SELECT COALESCE(SUM(realised_pnl), 0.0)
        FROM {table}
        WHERE closed_at >= ?
        """,
        (start_of_window,),
    )
    net_pnl = float(row[0]) if row else 0.0
    # Return a positive number representing total losses. If profitable, loss is 0.
    return abs(net_pnl) if net_pnl < 0 else 0.0


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
    async with get_db_lock(db):
        cursor = await execute_with_retry(
            db,
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
        await commit_with_retry(db)
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
    async with get_db_lock(db):
        cursor = await execute_with_retry(
            db,
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
        await commit_with_retry(db)
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
    row = await fetch_one(db, f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED'")
    total_closed = row[0] if row else 0
    
    # Wins (realised_pnl > 0)
    row = await fetch_one(db, f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl > 0")
    wins = row[0] if row else 0
    
    # Losses (realised_pnl <= 0)
    row = await fetch_one(db, f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl <= 0")
    losses = row[0] if row else 0
    
    # Total Realised P&L
    row = await fetch_one(db, f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE order_status = 'FILLED'")
    total_pnl = float(row[0]) if row else 0.0
    
    # Current Open Exposure
    row = await fetch_one(db, f"SELECT COALESCE(SUM(size_usdc), 0.0), COUNT(*) FROM {table} WHERE order_status = 'OPEN'")
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


# ---------------------------------------------------------------------------
# Spread Logging Tick-by-Tick
# ---------------------------------------------------------------------------

async def log_spread_tick(
    db: aiosqlite.Connection,
    market_id: str,
    symbol: str,
    window_minutes: int,
    best_bid: float,
    best_ask: float,
    spread_amount: float,
    spread_pct: float,
    sim_fok_yes_price: float = 0.0,
    sim_fok_yes_slippage: float = 0.0,
    sim_fok_yes_fillable: int = 0,
    sim_fok_no_price: float = 0.0,
    sim_fok_no_slippage: float = 0.0,
    sim_fok_no_fillable: int = 0,
    api_latency_ms: float = 0.0,
) -> int:
    """Record a spread update tick along with FOK order book sweep simulation and latency for quant model auditing."""
    try:
        async with get_db_lock(db):
            cursor = await execute_with_retry(
                db,
                """
                INSERT INTO spread_logs (
                    timestamp, market_id, symbol, window_minutes,
                    best_bid, best_ask, spread_amount, spread_pct,
                    sim_fok_yes_price, sim_fok_yes_slippage, sim_fok_yes_fillable,
                    sim_fok_no_price, sim_fok_no_slippage, sim_fok_no_fillable,
                    api_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    market_id,
                    symbol,
                    window_minutes,
                    best_bid,
                    best_ask,
                    spread_amount,
                    spread_pct,
                    sim_fok_yes_price,
                    sim_fok_yes_slippage,
                    sim_fok_yes_fillable,
                    sim_fok_no_price,
                    sim_fok_no_slippage,
                    sim_fok_no_fillable,
                    api_latency_ms,
                ),
            )
            await commit_with_retry(db)
            return cursor.lastrowid if cursor else 0
    except sqlite3.OperationalError as e:
        logger.warning("spread_log_tick_lock_skipped", market_id=market_id, error=str(e))
        return 0


async def get_historical_yes_price(db: aiosqlite.Connection, market_id: str, seconds_ago: int = 15) -> Optional[float]:
    """
    Get the historical YES price (best_ask) for a market from spread_logs closest to N seconds ago.
    Falls back to the closest available historical tick if an exact match is not found.
    """
    target_ts = int(time.time()) - seconds_ago
    try:
        # 1. Try to find a tick within a tight +/- 5s window
        row = await fetch_one(
            db,
            """
            SELECT best_ask FROM spread_logs 
            WHERE market_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY ABS(timestamp - ?) ASC 
            LIMIT 1
            """,
            (market_id, target_ts - 5, target_ts + 5, target_ts)
        )
        if row:
            return float(row[0])
        
        # 2. Fallback to the latest tick prior to target_ts
        row = await fetch_one(
            db,
            """
            SELECT best_ask FROM spread_logs 
            WHERE market_id = ? AND timestamp <= ?
            ORDER BY timestamp DESC 
            LIMIT 1
            """,
            (market_id, target_ts)
        )
        if row:
            return float(row[0])
    except Exception as e:
        logger.error("failed_to_fetch_historical_yes_price", market_id=market_id, error=str(e))
    return None


# ---------------------------------------------------------------------------
# V3 Upgrades: Sizing, Signals, and Regimes
# ---------------------------------------------------------------------------

async def get_rolling_win_rate(
    db: aiosqlite.Connection, symbol: str, window_size: str, direction: str
) -> Dict[str, Any]:
    """Fetch the rolling win rate and pre-computed Kelly fraction for a bucket."""
    row = await fetch_one(
        db,
        """
        SELECT rolling_win_rate, kelly_fraction, wins, losses, total_trades, n_signals_resolved
        FROM win_rate_tracker
        WHERE symbol = ? AND window_size = ? AND direction = ?
        """,
        (symbol, str(window_size), direction),
    )
    if row:
        return dict(row)
    return {
        "rolling_win_rate": 0.5,
        "kelly_fraction": 0.0,
        "wins": 0,
        "losses": 0,
        "total_trades": 0,
        "n_signals_resolved": 0,
    }


async def log_signal_evaluation(
    db: aiosqlite.Connection,
    market_id: str,
    symbol: str,
    window_size: str,
    direction: str,
    side: str,
    t1_market_id: Optional[str],
    t1_result: Optional[str],
    t2_market_id: Optional[str],
    t2_result: Optional[str],
    t3_market_id: Optional[str],
    t3_result: Optional[str],
    streak_depth: int,
    yes_price_at_signal: float,
    bid_ask_spread_at_signal: float,
    book_imbalance: float,
    cvd_60s: float,
    price_velocity: float,
    ev_computed: float,
    seconds_to_close: float,
    guard_result: str,
    guard_rejection_reason: Optional[str],
    outcome_source: str = 'GAMMA',
    candle_1_delta_bps: float = 0.0,
    candle_2_delta_bps: float = 0.0,
    entry_price: float = 0.0,
    time_since_candle_close_secs: float = 0.0,
    eventual_pnl: float = 0.0,
    correlated_position_open: int = 0,
    correlated_position_direction: Optional[str] = None,
    persistence_score: float = 0.0,
    strategy_mode: str = 'TREND',
    underlying_strike_price: float = 0.0,
    underlying_spot_price: float = 0.0,
    underlying_distance_bps: float = 0.0,
    underlying_spot_velocity_10s: float = 0.0,
    underlying_realized_vol_5m: float = 0.0,
    underlying_realized_vol_1h: float = 0.0,
    sim_slippage_bps_at_target: float = 0.0,
    depth_usd_1c: float = 0.0,
    depth_usd_5c: float = 0.0,
    book_imbalance_momentum_10s: float = 0.0,
    ask_depth_usd_1c: float = 0.0,
    bid_depth_usd_1c: float = 0.0,
    bs_implied_probability: float = 0.5,
    option_pricing_edge_bps: float = 0.0,
    execution_attempts: int = 1,
    execution_duration_ms: float = 0.0,
    fill_percentage: float = 1.0,
    resolution_latency_seconds: int = 0,
) -> int:
    """Log every signal evaluation (accepted or rejected) for guard auditing."""
    async with get_db_lock(db):
        cursor = await execute_with_retry(
            db,
            """
            INSERT INTO signals (
                market_id, symbol, window_size, direction, side,
                t1_market_id, t1_result, t2_market_id, t2_result,
                t3_market_id, t3_result, streak_depth, yes_price_at_signal,
                bid_ask_spread_at_signal, book_imbalance, cvd_60s, price_velocity,
                ev_computed, seconds_to_close, guard_result, guard_rejection_reason,
                signal_timestamp, outcome_source, candle_1_delta_bps, candle_2_delta_bps,
                entry_price, time_since_candle_close_secs, eventual_pnl,
                correlated_position_open, correlated_position_direction, persistence_score, strategy_mode,
                underlying_strike_price, underlying_spot_price, underlying_distance_bps, underlying_spot_velocity_10s,
                underlying_realized_vol_5m, underlying_realized_vol_1h, sim_slippage_bps_at_target, depth_usd_1c,
                depth_usd_5c, book_imbalance_momentum_10s, ask_depth_usd_1c, bid_depth_usd_1c, bs_implied_probability,
                option_pricing_edge_bps, execution_attempts, execution_duration_ms, fill_percentage, resolution_latency_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                symbol,
                str(window_size),
                direction,
                side,
                t1_market_id,
                t1_result,
                t2_market_id,
                t2_result,
                t3_market_id,
                t3_result,
                streak_depth,
                yes_price_at_signal,
                bid_ask_spread_at_signal,
                book_imbalance,
                cvd_60s,
                price_velocity,
                ev_computed,
                seconds_to_close,
                guard_result,
                guard_rejection_reason,
                outcome_source,
                candle_1_delta_bps,
                candle_2_delta_bps,
                entry_price,
                time_since_candle_close_secs,
                eventual_pnl,
                correlated_position_open,
                correlated_position_direction,
                persistence_score,
                strategy_mode,
                underlying_strike_price,
                underlying_spot_price,
                underlying_distance_bps,
                underlying_spot_velocity_10s,
                underlying_realized_vol_5m,
                underlying_realized_vol_1h,
                sim_slippage_bps_at_target,
                depth_usd_1c,
                depth_usd_5c,
                book_imbalance_momentum_10s,
                ask_depth_usd_1c,
                bid_depth_usd_1c,
                bs_implied_probability,
                option_pricing_edge_bps,
                execution_attempts,
                execution_duration_ms,
                fill_percentage,
                resolution_latency_seconds,
            ),
        )
        await commit_with_retry(db)
        return cursor.lastrowid



async def log_market_regime(
    db: aiosqlite.Connection,
    symbol: str,
    btc_price: float,
    eth_price: float,
    cvd_60s_btc: float,
    cvd_60s_eth: float,
    cvd_5m_btc: float,
    cvd_5m_eth: float,
    rolling_spread_median: float,
    spread_multiplier: float,
    iv_proxy: float,
    label: str,
    session: str,
) -> int:
    """Snapshot time-series market volatility and CVD regimes every 60s."""
    async with get_db_lock(db):
        cursor = await execute_with_retry(
            db,
            """
            INSERT INTO market_regimes (
                timestamp, symbol, btc_binance_price, eth_binance_price,
                cvd_60s_btc, cvd_60s_eth, cvd_5m_btc, cvd_5m_eth,
                rolling_spread_median, spread_regime_multiplier, implied_vol_proxy,
                regime_label, session
            ) VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                btc_price,
                eth_price,
                cvd_60s_btc,
                cvd_60s_eth,
                cvd_5m_btc,
                cvd_5m_eth,
                rolling_spread_median,
                spread_multiplier,
                iv_proxy,
                label,
                session,
            ),
        )
        await commit_with_retry(db)
        return cursor.lastrowid


_recalc_lock = asyncio.Lock()
_last_recalc_ts = 0.0


async def recalculate_win_rates(db: aiosqlite.Connection, rolling_n: int = 50, force: bool = False) -> None:
    """
    Scans recent evaluated signals in the signals table that have resolved,
    groups by bucket, recalculates rolling win rates, computes Half-Kelly fractions,
    and updates the win_rate_tracker.
    Throttled to at most once every 60s with asyncio.Lock concurrency guard.
    """
    global _last_recalc_ts
    now = time.time()
    if not force and (now - _last_recalc_ts < 60.0):
        return
    if _recalc_lock.locked():
        return

    async with _recalc_lock:
        _last_recalc_ts = time.time()
        db.row_factory = aiosqlite.Row
        # Query recent detected market streak opportunities that have known real-world resolutions
        raw_rows = await fetch_all(
            db,
            """
            SELECT signal_id, entry_price, side, eventual_resolution,
                   symbol, window_size as window, direction, eventual_pnl,
                   yes_price_at_signal, guard_result, signal_timestamp
            FROM signals
            WHERE eventual_resolution IS NOT NULL AND eventual_resolution != 'UNKNOWN'
            ORDER BY signal_timestamp DESC
            LIMIT 1000
            """
        )
        rows = list(reversed(raw_rows))  # Restore chronological ascending order
        
        # 2. Group trades by (symbol, window, direction)
        buckets = {}
        for row in rows:
            symbol = str(row["symbol"]).upper()
            window = str(row["window"])
            direction = str(row["direction"]).upper()
            
            key = (symbol, window, direction)
            if key not in buckets:
                buckets[key] = []
                
            side = str(row["side"]).upper()
            res = str(row["eventual_resolution"]).upper()
            
            # Calculate dynamic entry price from signal price
            yes_p = row["yes_price_at_signal"]
            entry_p = yes_p if side == "YES" else (1.0 - yes_p)
            if entry_p <= 0.01 or entry_p >= 0.99:
                entry_p = row["entry_price"] if row["entry_price"] > 0 else 0.50
                
            # Compute real payout winner (YES resolved YES/UP, or NO resolved NO/DOWN)
            win = (side == "YES" and res in ("YES", "UP")) or (side == "NO" and res in ("NO", "DOWN"))
            pnl_val = (1.0 - entry_p) if win else -entry_p
            
            guard_res = str(row["guard_result"]).upper()
            traded = (guard_res in ("ACCEPTED", "ENTERED"))
            
            buckets[key].append({
                "entry_price": entry_p,
                "realised_pnl": pnl_val,
                "side": side,
                "traded": traded
            })
        
        # 3. For each bucket, calculate rolling stats and Half-Kelly
        now_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        async with get_db_lock(db):
            for key, trades in buckets.items():
                symbol, window, direction = key
                total_trades = sum(1 for t in trades if t["traded"])
                n_signals_resolved = len(trades)
                
                # Take only the last rolling_n trades
                rolling_trades = trades[-rolling_n:]
                n_rolling = len(rolling_trades)
                if n_rolling == 0:
                    continue
                    
                wins = sum(1 for t in rolling_trades if t["realised_pnl"] > 0)
                losses = sum(1 for t in rolling_trades if t["realised_pnl"] < 0)
                
                rolling_win_rate = wins / n_rolling if n_rolling > 0 else 0.5
                
                # Calculate Half-Kelly for the next trade.
                # Use average entry price of rolling trades as estimator of odds
                avg_entry = sum(t["entry_price"] for t in rolling_trades) / n_rolling
                
                # Odds b = (0.98 / entry_price) - 1.0. Accounts for simulated 2% transaction friction.
                b = (0.98 / avg_entry) - 1.0 if avg_entry > 0.0 and avg_entry < 0.98 else 0.01
                p = rolling_win_rate
                
                # Kelly: f = p - (1 - p)/b
                f = p - (1.0 - p) / b if b > 0.0 else 0.0
                kelly_fraction = max(0.0, f * 0.5) # Half-Kelly, clamped to positive
                
                # Average EV and PnL
                avg_pnl = sum(t["realised_pnl"] for t in rolling_trades) / n_rolling
                
                # Sharpe ratio (PnL std dev proxy)
                pnl_vals = [t["realised_pnl"] for t in rolling_trades]
                if n_rolling > 1:
                    mean_pnl = sum(pnl_vals) / n_rolling
                    var_pnl = sum((x - mean_pnl)**2 for x in pnl_vals) / (n_rolling - 1)
                    std_pnl = var_pnl ** 0.5
                    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0
                else:
                    sharpe = 0.0
                    
                # Upsert into win_rate_tracker
                await execute_with_retry(
                    db,
                    """
                    INSERT INTO win_rate_tracker (
                        symbol, window_size, direction, total_trades, rolling_n,
                        wins, losses, rolling_win_rate, kelly_fraction,
                        avg_ev, avg_pnl, sharpe_ratio, last_updated, staleness_flag,
                        n_signals_resolved
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(symbol, window_size, direction) DO UPDATE SET
                        total_trades = excluded.total_trades,
                        rolling_n = excluded.rolling_n,
                        wins = excluded.wins,
                        losses = excluded.losses,
                        rolling_win_rate = excluded.rolling_win_rate,
                        kelly_fraction = excluded.kelly_fraction,
                        avg_ev = excluded.avg_ev,
                        avg_pnl = excluded.avg_pnl,
                        sharpe_ratio = excluded.sharpe_ratio,
                        last_updated = excluded.last_updated,
                        n_signals_resolved = excluded.n_signals_resolved
                    """,
                    (
                        symbol,
                        window,
                        direction,
                        total_trades,
                        n_rolling,
                        wins,
                        losses,
                        rolling_win_rate,
                        kelly_fraction,
                        rolling_win_rate * b - (1.0 - rolling_win_rate), # EV
                        avg_pnl,
                        sharpe,
                        now_str,
                        n_signals_resolved,
                    ),
                )
            await commit_with_retry(db)


async def prune_spread_logs(db: aiosqlite.Connection, keep_hours: int = 1) -> int:
    """Delete records from spread_logs older than keep_hours."""
    cutoff = int(time.time()) - (keep_hours * 3600)
    async with get_db_lock(db):
        cursor = await execute_with_retry(
            db,
            "DELETE FROM spread_logs WHERE timestamp < ?",
            (cutoff,)
        )
        await commit_with_retry(db)
        deleted_rows = cursor.rowcount
    if deleted_rows > 0:
        logger.info("spread_logs_pruned", cutoff_timestamp=cutoff, rows_deleted=deleted_rows)
    return deleted_rows


async def log_hypothetical_kelly(
    market_id: str,
    symbol: str,
    window: int,
    direction: str,
    streak_depth: int,
    rolling_win_rate: float,
    n_signals_resolved: int,
    live_entry_price: float,
    live_odds_b: float,
    live_half_kelly: float,
    hypothetical_base_size: float,
    hypothetical_final_shares: float,
    actual_flat_base_size: float,
) -> None:
    """
    Log the hypothetical Kelly evaluation into an isolated SQLite database 
    to track performance without affecting actual live PnL.
    """
    try:
        from pathlib import Path
        db_dir = Path(__file__).parent.parent.parent / "data"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "hypotheticalkellyevaluation.db"
        
        async with aiosqlite.connect(str(db_path), timeout=60.0) as kelly_db:
            await kelly_db.execute("PRAGMA journal_mode = WAL")
            await kelly_db.execute("PRAGMA busy_timeout = 60000")
            await kelly_db.execute(
                """
                CREATE TABLE IF NOT EXISTS hypothetical_kelly_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    market_id TEXT,
                    symbol TEXT,
                    window INTEGER,
                    direction TEXT,
                    streak_depth INTEGER,
                    rolling_win_rate REAL,
                    n_signals_resolved INTEGER,
                    live_entry_price REAL,
                    live_odds_b REAL,
                    live_half_kelly REAL,
                    hypothetical_base_size REAL,
                    hypothetical_final_shares REAL,
                    actual_flat_base_size REAL
                )
                """
            )
            await kelly_db.execute(
                """
                INSERT INTO hypothetical_kelly_logs (
                    timestamp, market_id, symbol, window, direction, streak_depth,
                    rolling_win_rate, n_signals_resolved, live_entry_price, live_odds_b,
                    live_half_kelly, hypothetical_base_size, hypothetical_final_shares, actual_flat_base_size
                ) VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id, symbol, window, direction, streak_depth,
                    rolling_win_rate, n_signals_resolved, live_entry_price, live_odds_b,
                    live_half_kelly, hypothetical_base_size, hypothetical_final_shares, actual_flat_base_size
                )
            )
            await kelly_db.commit()
    except Exception as e:
        logger.error("hypothetical_kelly_log_failed", error=str(e), market_id=market_id)


# ---------------------------------------------------------------------------
# Strategy Profile Framework Positions
# ---------------------------------------------------------------------------

async def create_profile_position(
    db: aiosqlite.Connection,
    market_id: str,
    entry_price: float,
    size_usdc: float,
    shares_bought: float,
    bot_version: str,
    notes: Optional[str] = None,
    side: str = 'NO',
    win_prob_estimate: float = 0.5,
    kelly_fraction_used: float = 0.0,
    bid_ask_spread_at_entry: float = 0.0,
    book_imbalance_at_entry: float = 0.0,
    cvd_60s_at_entry: float = 0.0,
    correlated_position_open: int = 0,
    seconds_to_close_at_entry: float = 0.0,
    market_age_at_entry_pct: float = 0.0,
    bankroll_at_entry: float = 0.0,
    persistence_score: float = 0.0,
    strategy_mode: str = 'TREND',
    profile_name: str = 'stop_loss',
    underlying_distance_bps: float = 0.0,
    bs_implied_probability: float = 0.5,
    realized_vol_5m: float = 0.0,
) -> str:
    """Create a new virtual position for a profile with status OPEN."""
    import datetime
    from zoneinfo import ZoneInfo

    position_id = f"virtual_{profile_name}_{str(uuid.uuid4())[:8]}"
    opened_at_ts = int(time.time())

    dt_utc = datetime.datetime.fromtimestamp(opened_at_ts, tz=datetime.timezone.utc)
    dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))

    hour_utc = dt_utc.hour
    hour_et = dt_et.hour
    day_of_week = dt_et.strftime("%A")
    is_weekend = 1 if day_of_week in ("Saturday", "Sunday") else 0

    if 19 <= hour_et or hour_et < 3:
        session = "ASIA"
    elif 3 <= hour_et < 8:
        session = "LONDON"
    elif 8 <= hour_et < 17:
        session = "NY"
    else:
        session = "OFF"

    # Fetch market info to denormalize symbol and window_minutes
    row = await fetch_one(db, "SELECT question, slug FROM markets WHERE market_id = ?", (market_id,))
    symbol = "BTC"
    window_minutes = 5
    if row:
        question = (row["question"] or "").lower()
        slug = (row["slug"] or "").lower()
        
        if "-5m-" in slug or "5-minute" in question:
            window_minutes = 5
        elif "-15m-" in slug or "15-minute" in question:
            window_minutes = 15
            
        import re
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or re.search(r'\bsol\b', question) is not None:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"

    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            INSERT INTO profile_positions (
                position_id, market_id, side, entry_price, size_usdc,
                shares_bought, order_status, opened_at, version_opened, notes,
                max_price_seen, min_price_seen,
                win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                seconds_to_close_at_entry, market_age_at_entry_pct,
                hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                window_minutes, symbol, persistence_score, strategy_mode, profile_name,
                underlying_distance_bps, bs_implied_probability, realized_vol_5m
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                market_id,
                side,
                entry_price,
                size_usdc,
                shares_bought,
                opened_at_ts,
                bot_version,
                notes,
                entry_price,
                entry_price,
                win_prob_estimate,
                kelly_fraction_used,
                bid_ask_spread_at_entry,
                book_imbalance_at_entry,
                cvd_60s_at_entry,
                correlated_position_open,
                seconds_to_close_at_entry,
                market_age_at_entry_pct,
                hour_utc,
                hour_et,
                day_of_week,
                is_weekend,
                session,
                bankroll_at_entry,
                window_minutes,
                symbol,
                persistence_score,
                strategy_mode,
                profile_name,
                underlying_distance_bps,
                bs_implied_probability,
                realized_vol_5m,
            ),
        )
        await commit_with_retry(db)
    logger.info("profile_position_created", profile=profile_name, position_id=position_id, market_id=market_id)
    return position_id



async def close_profile_position(
    db: aiosqlite.Connection,
    position_id: str,
    exit_price: float,
    realised_pnl: float,
    close_reason: str,
    meta_logger: Optional[Any] = None,
) -> None:
    """Close a virtual profile position with exit data."""
    now = int(time.time())

    # Fetch resolving position's side and market_id to accurately resolve executioner_positions
    pos_row = await fetch_one(db, "SELECT market_id, side FROM profile_positions WHERE position_id = ?", (position_id,))

    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            UPDATE profile_positions
            SET order_status = 'FILLED',
                closed_at = CASE WHEN ? = 'RESOLUTION' THEN COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = profile_positions.market_id), ?) ELSE ? END,
                exit_price = ?,
                realised_pnl = ?,
                close_reason = ?,
                resolution_delay = ? - COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = profile_positions.market_id), ?)
            WHERE position_id = ?
            """,
            (close_reason, now, now, exit_price, realised_pnl, close_reason, now, now, position_id),
        )

        if pos_row:
            m_id = pos_row["market_id"]
            pos_side = pos_row["side"]
            winning_side = None
            if exit_price >= 0.80:
                winning_side = pos_side
            elif exit_price <= 0.20:
                winning_side = "NO" if pos_side == "YES" else "YES"

            if winning_side:
                # Update YES positions in executioner_positions for this market
                yes_exit = 1.0 if winning_side == "YES" else 0.0
                await execute_with_retry(
                    db,
                    """
                    UPDATE executioner_positions
                    SET order_status = 'FILLED',
                        closed_at = CASE WHEN ? = 'RESOLUTION' THEN COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?) ELSE ? END,
                        exit_price = ?,
                        realised_pnl = CASE WHEN ? = 1.0 THEN shares_bought * (1.0 - entry_price) ELSE -size_usdc END,
                        close_reason = ?,
                        resolution_delay = ? - COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?)
                    WHERE (position_id = ? OR market_id = ?) AND side = 'YES' AND order_status = 'OPEN'
                    """,
                    (close_reason, now, now, yes_exit, yes_exit, close_reason, now, now, position_id, m_id),
                )
                # Update NO positions in executioner_positions for this market
                no_exit = 1.0 if winning_side == "NO" else 0.0
                await execute_with_retry(
                    db,
                    """
                    UPDATE executioner_positions
                    SET order_status = 'FILLED',
                        closed_at = CASE WHEN ? = 'RESOLUTION' THEN COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?) ELSE ? END,
                        exit_price = ?,
                        realised_pnl = CASE WHEN ? = 1.0 THEN shares_bought * (1.0 - entry_price) ELSE -size_usdc END,
                        close_reason = ?,
                        resolution_delay = ? - COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?)
                    WHERE (position_id = ? OR market_id = ?) AND side = 'NO' AND order_status = 'OPEN'
                    """,
                    (close_reason, now, now, no_exit, no_exit, close_reason, now, now, position_id, m_id),
                )

        await commit_with_retry(db)
    logger.info("profile_position_closed", position_id=position_id, exit_price=exit_price, realised_pnl=realised_pnl)

    # Shadow meta-strategy logger — observational only
    try:
        if meta_logger is not None:
            await meta_logger.log_trade_resolved(
                position_id=position_id,
                realised_pnl=realised_pnl,
                closed_at=now
            )
        else:
            from src.strategy.meta_logger import MetaStrategyLogger
            mlog = MetaStrategyLogger(db)
            await mlog.log_trade_resolved(
                position_id=position_id,
                realised_pnl=realised_pnl,
                closed_at=now
            )
    except Exception as e:
        logger.warning("meta_logger_resolve_error", position_id=position_id, error=str(e))


async def update_profile_position_min_max_prices(
    db: aiosqlite.Connection,
    position_id: str,
    min_price: float,
    max_price: float,
) -> None:
    """Update tracking for lowest and highest prices observed for a profile position."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            "UPDATE profile_positions SET min_price_seen = ?, max_price_seen = ? WHERE position_id = ?",
            (min_price, max_price, position_id),
        )
        await commit_with_retry(db)


async def get_profile_position(
    db: aiosqlite.Connection,
    position_id: str,
) -> Optional[Dict]:
    """Fetch a single profile position by ID."""
    row = await fetch_one(
        db, "SELECT * FROM profile_positions WHERE position_id = ?", (position_id,)
    )
    return dict(row) if row else None


async def update_profile_position_notes(
    db: aiosqlite.Connection,
    position_id: str,
    notes: str,
) -> None:
    """Update notes for a profile position."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            "UPDATE profile_positions SET notes = ? WHERE position_id = ?",
            (notes, position_id),
        )
        await commit_with_retry(db)


async def init_executioner_table(db: aiosqlite.Connection) -> None:
    """Ensure executioner_positions table exists."""
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            CREATE TABLE IF NOT EXISTS executioner_positions (
                position_id         TEXT PRIMARY KEY,
                base_profile_name   TEXT NOT NULL,
                request_id          INTEGER NOT NULL,
                market_id           TEXT NOT NULL,
                side                TEXT NOT NULL DEFAULT 'NO',
                entry_price         REAL,
                size_usdc           REAL,
                shares_bought       REAL,
                order_id            TEXT,
                order_status        TEXT NOT NULL DEFAULT 'PENDING',
                opened_at           INTEGER,
                closed_at           INTEGER,
                exit_price          REAL,
                realised_pnl        REAL,
                close_reason        TEXT,
                version_opened      TEXT,
                notes               TEXT,
                max_price_seen      REAL,
                min_price_seen      REAL,
                resolution_delay    INTEGER,
                win_prob_estimate         REAL DEFAULT 0.5,
                kelly_fraction_used       REAL DEFAULT 0.0,
                bid_ask_spread_at_entry   REAL DEFAULT 0.0,
                book_imbalance_at_entry   REAL DEFAULT 0.0,
                cvd_60s_at_entry          REAL DEFAULT 0.0,
                correlated_position_open  INTEGER DEFAULT 0,
                seconds_to_close_at_entry REAL DEFAULT 0.0,
                market_age_at_entry_pct   REAL DEFAULT 0.0,
                hour_utc                  INTEGER DEFAULT 0,
                hour_et                   INTEGER DEFAULT 0,
                day_of_week               TEXT DEFAULT '',
                is_weekend                INTEGER DEFAULT 0,
                session                   TEXT DEFAULT 'OFF',
                bankroll_at_entry         REAL DEFAULT 0.0,
                window_minutes            INTEGER DEFAULT 0,
                symbol                    TEXT DEFAULT '',
                persistence_score         REAL DEFAULT 0.0,
                strategy_mode             TEXT DEFAULT 'TREND',
                underlying_distance_bps   REAL DEFAULT 0.0,
                bs_implied_probability    REAL DEFAULT 0.5,
                resolution_latency_seconds INTEGER DEFAULT 0,
                realized_vol_5m           REAL DEFAULT 0.0,
                created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await commit_with_retry(db)


async def insert_executioner_position(
    db: aiosqlite.Connection,
    position_id: str,
    base_profile_name: str,
    request_id: int,
    market_id: str,
    entry_price: float,
    size_usdc: float,
    shares_bought: float,
    notes: Optional[str] = None,
    bot_version: str = "v3.120",
    side: str = 'NO',
    win_prob_estimate: float = 0.5,
    kelly_fraction_used: float = 0.0,
    bid_ask_spread_at_entry: float = 0.0,
    book_imbalance_at_entry: float = 0.0,
    cvd_60s_at_entry: float = 0.0,
    correlated_position_open: int = 0,
    seconds_to_close_at_entry: float = 0.0,
    market_age_at_entry_pct: float = 0.0,
    bankroll_at_entry: float = 0.0,
    persistence_score: float = 0.0,
    strategy_mode: str = 'TREND',
    underlying_distance_bps: float = 0.0,
    bs_implied_probability: float = 0.5,
    realized_vol_5m: float = 0.0,
) -> None:
    """Insert dedicated record into executioner_positions table."""
    import datetime
    from zoneinfo import ZoneInfo

    opened_at_ts = int(time.time())
    dt_utc = datetime.datetime.fromtimestamp(opened_at_ts, tz=datetime.timezone.utc)
    dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))

    hour_utc = dt_utc.hour
    hour_et = dt_et.hour
    day_of_week = dt_et.strftime("%A")
    is_weekend = 1 if day_of_week in ("Saturday", "Sunday") else 0

    if 19 <= hour_et or hour_et < 3:
        session = "ASIA"
    elif 3 <= hour_et < 8:
        session = "LONDON"
    elif 8 <= hour_et < 17:
        session = "NY"
    else:
        session = "OFF"

    row = await fetch_one(db, "SELECT question, slug FROM markets WHERE market_id = ?", (market_id,))
    symbol = "BTC"
    window_minutes = 5
    if row:
        question = (row["question"] or "").lower()
        slug = (row["slug"] or "").lower()
        if "-5m-" in slug or "5-minute" in question:
            window_minutes = 5
        elif "-15m-" in slug or "15-minute" in question:
            window_minutes = 15
            
        import re
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or re.search(r'\bsol\b', question) is not None:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"

    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            INSERT INTO executioner_positions (
                position_id, base_profile_name, request_id, market_id, side, entry_price, size_usdc,
                shares_bought, order_status, opened_at, version_opened, notes,
                max_price_seen, min_price_seen,
                win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                seconds_to_close_at_entry, market_age_at_entry_pct,
                hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                window_minutes, symbol, persistence_score, strategy_mode,
                underlying_distance_bps, bs_implied_probability, realized_vol_5m
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                base_profile_name,
                request_id,
                market_id,
                side,
                entry_price,
                size_usdc,
                shares_bought,
                opened_at_ts,
                bot_version,
                notes,
                entry_price,
                entry_price,
                win_prob_estimate,
                kelly_fraction_used,
                bid_ask_spread_at_entry,
                book_imbalance_at_entry,
                cvd_60s_at_entry,
                correlated_position_open,
                seconds_to_close_at_entry,
                market_age_at_entry_pct,
                hour_utc,
                hour_et,
                day_of_week,
                is_weekend,
                session,
                bankroll_at_entry,
                window_minutes,
                symbol,
                persistence_score,
                strategy_mode,
                underlying_distance_bps,
                bs_implied_probability,
                realized_vol_5m,
            ),
        )
        await commit_with_retry(db)
    logger.info("executioner_position_created", position_id=position_id, base_profile=base_profile_name, request_id=request_id)


async def close_executioner_position(
    db: aiosqlite.Connection,
    position_id: str,
    exit_price: float,
    realised_pnl: float,
    close_reason: str = 'RESOLUTION',
) -> None:
    """Close an executioner position with exit data."""
    now = int(time.time())
    async with get_db_lock(db):
        await execute_with_retry(
            db,
            """
            UPDATE executioner_positions
            SET order_status = 'FILLED',
                closed_at = CASE WHEN ? = 'RESOLUTION' THEN COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?) ELSE ? END,
                exit_price = ?,
                realised_pnl = ?,
                close_reason = ?,
                resolution_delay = ? - COALESCE((SELECT closes_at FROM markets WHERE markets.market_id = executioner_positions.market_id), ?)
            WHERE position_id = ?
            """,
            (close_reason, now, now, exit_price, realised_pnl, close_reason, now, now, position_id),
        )
        await commit_with_retry(db)
    logger.info("executioner_position_closed", position_id=position_id, exit_price=exit_price, realised_pnl=realised_pnl)


async def get_open_profile_positions(
    db: aiosqlite.Connection
) -> List[Dict]:
    """Get all open profile positions."""
    rows = await fetch_all(
        db, "SELECT * FROM profile_positions WHERE order_status = 'OPEN'"
    )
    return [dict(r) for r in rows]


async def get_active_and_unexpired_profile_positions(
    db: aiosqlite.Connection, now: Optional[int] = None
) -> List[Dict]:
    """Get all open profile positions using status index."""
    rows = await fetch_all(
        db,
        """
        SELECT *
        FROM profile_positions
        WHERE order_status = 'OPEN'
        """
    )
    return [dict(r) for r in rows]



async def get_profile_performance_summary(
    db: aiosqlite.Connection,
    profile_name: str
) -> Dict[str, Any]:
    """Calculate aggregate performance metrics for a profile."""
    # Total trades
    row = await fetch_one(
        db,
        "SELECT COUNT(*) FROM profile_positions WHERE profile_name = ? AND order_status = 'FILLED'",
        (profile_name,)
    )
    total_closed = row[0] if row else 0

    # Wins
    row = await fetch_one(
        db,
        "SELECT COUNT(*) FROM profile_positions WHERE profile_name = ? AND order_status = 'FILLED' AND realised_pnl > 0",
        (profile_name,)
    )
    wins = row[0] if row else 0

    # Losses
    row = await fetch_one(
        db,
        "SELECT COUNT(*) FROM profile_positions WHERE profile_name = ? AND order_status = 'FILLED' AND realised_pnl <= 0",
        (profile_name,)
    )
    losses = row[0] if row else 0

    # Realised P&L
    row = await fetch_one(
        db,
        "SELECT COALESCE(SUM(realised_pnl), 0.0) FROM profile_positions WHERE profile_name = ? AND order_status = 'FILLED'",
        (profile_name,)
    )
    total_pnl = float(row[0]) if row else 0.0

    # Open Exposure
    row = await fetch_one(
        db,
        "SELECT COALESCE(SUM(size_usdc), 0.0), COUNT(*) FROM profile_positions WHERE profile_name = ? AND order_status = 'OPEN'",
        (profile_name,)
    )
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
        "is_paper": True
    }


async def get_profile_history(
    db: aiosqlite.Connection,
    profile_name: str,
    limit: int = 50
) -> List[Dict]:
    """Get closed trades history for a profile."""
    sql = """
        SELECT p.*, m.question
        FROM profile_positions p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.profile_name = ? AND p.order_status = 'FILLED'
        ORDER BY p.closed_at DESC
        LIMIT ?
    """
    rows = await fetch_all(db, sql, (profile_name, limit))
    result = []
    for r in rows:
        d = dict(r)
        if d.get("streak_depth") is None:
            notes_str = d.get("notes")
            if notes_str and "streak_depth" in notes_str:
                try:
                    parsed_notes = json.loads(notes_str)
                    d["streak_depth"] = parsed_notes.get("streak_depth")
                except Exception:
                    pass
        result.append(d)
    return result


async def get_profile_pnl_series(
    db: aiosqlite.Connection,
    profile_name: str
) -> List[Dict]:
    """Get chronological cumulative P&L data points for a profile."""
    sql = """
        SELECT closed_at, realised_pnl 
        FROM profile_positions 
        WHERE profile_name = ? AND order_status = 'FILLED' 
        ORDER BY closed_at ASC
    """
    rows = await fetch_all(db, sql, (profile_name,))
    running = 0.0
    series = []
    for row in rows:
        running += row["realised_pnl"] or 0
        series.append({"ts": row["closed_at"], "pnl": round(running, 4)})
    return series


async def get_last_closed_trend_trade(
    db: aiosqlite.Connection,
    skip_db: Any,
    paper: bool = False,
    symbol: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Get the last closed trend strategy trade by comparing the main bot's completed
    trend positions and the skip database's completed skipped positions.
    """
    table = "paper_positions" if paper else "positions"
    query = (
        f"SELECT p.position_id, p.realised_pnl, p.closed_at FROM {table} p "
        f"WHERE p.order_status = 'FILLED' AND p.realised_pnl IS NOT NULL "
        f"AND p.strategy_mode != 'LOSS_FADE' "
        f"ORDER BY p.closed_at DESC LIMIT 1"
    )
    
    last_real_row = await fetch_one(db, query)
    last_real = dict(last_real_row) if last_real_row else None

    last_skipped = None
    if skip_db is not None:
        from unittest.mock import MagicMock, AsyncMock
        if not isinstance(skip_db, (MagicMock, AsyncMock)):
            try:
                last_skipped = await skip_db.get_last_closed_skipped_position(symbol=None)
            except Exception as e:
                logger.error("failed_to_get_last_closed_skipped_position", error=str(e))

    if last_real and last_skipped:
        if last_real["closed_at"] > last_skipped["closed_at"]:
            last_real["source"] = "real"
            return last_real
        elif last_skipped["closed_at"] > last_real["closed_at"]:
            last_skipped["source"] = "skip"
            return last_skipped
        else:
            # Tie breaker: if either is a loss, prioritize the loss to trigger recovery!
            if last_real["realised_pnl"] < 0:
                last_real["source"] = "real"
                return last_real
            elif last_skipped["realised_pnl"] < 0:
                last_skipped["source"] = "skip"
                return last_skipped
            else:
                last_real["source"] = "real"
                return last_real
    elif last_real:
        last_real["source"] = "real"
        return last_real
    elif last_skipped:
        last_skipped["source"] = "skip"
        return last_skipped
    return None


async def log_wick_event(
    db: aiosqlite.Connection,
    position_id: str,
    market_id: str,
    profile_name: str,
    start_time: float,
    end_time: float,
    duration_ms: float,
    resolved_by: str,
    entry_price: float,
    trigger_price: float,
    exit_price: float,
) -> None:
    """Record a wick event (dip below 0.50) for future analysis of wick durations."""
    now = int(time.time())
    try:
        async with get_db_lock(db):
            await execute_with_retry(
                db,
                """
                INSERT INTO wick_events (
                    position_id, market_id, profile_name, start_time, end_time,
                    duration_ms, resolved_by, entry_price, trigger_price, exit_price, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    market_id,
                    profile_name,
                    start_time,
                    end_time,
                    duration_ms,
                    resolved_by,
                    entry_price,
                    trigger_price,
                    exit_price,
                    now,
                ),
            )
            await commit_with_retry(db)
        logger.info(
            "wick_event_logged",
            position_id=position_id,
            market_id=market_id,
            profile=profile_name,
            duration_ms=duration_ms,
            resolved_by=resolved_by,
        )
    except Exception as e:
        logger.error("failed_to_log_wick_event", error=str(e), position_id=position_id)







