"""
Database query module for Polymarket Reverse Bot.
All SQL queries are parameterized — NEVER string-format SQL.
"""

import time
import uuid
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiosqlite
from src.logs.log_config import get_logger

logger = get_logger("db")


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
    cursor = await db.execute("SELECT question, slug FROM markets WHERE market_id = ?", (market_id,))
    row = await cursor.fetchone()
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
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or "sol" in question:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"

    if paper:
        await db.execute(
            """
            INSERT INTO paper_positions (
                position_id, market_id, side, entry_price, size_usdc,
                shares_bought, order_status, opened_at, version_opened, notes,
                max_price_seen, min_price_seen,
                win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                seconds_to_close_at_entry, market_age_at_entry_pct,
                hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                window_minutes, symbol
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
    else:
        await db.execute(
            """
            INSERT INTO positions (
                position_id, market_id, side, entry_price, size_usdc,
                shares_bought, order_status, opened_at, version_opened, notes,
                max_price_seen, min_price_seen,
                win_prob_estimate, kelly_fraction_used, bid_ask_spread_at_entry,
                book_imbalance_at_entry, cvd_60s_at_entry, correlated_position_open,
                seconds_to_close_at_entry, market_age_at_entry_pct,
                hour_utc, hour_et, day_of_week, is_weekend, session, bankroll_at_entry,
                window_minutes, symbol
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


async def update_position_notes(
    db: aiosqlite.Connection,
    position_id: str,
    notes: str,
    paper: bool = False,
) -> None:
    """Update the notes field for a position (used for self-healing Token IDs)."""
    table = "paper_positions" if paper else "positions"
    await db.execute(
        f"UPDATE {table} SET notes = ? WHERE position_id = ?",
        (notes, position_id),
    )
    await db.commit()


async def update_position_min_max_prices(
    db: aiosqlite.Connection,
    position_id: str,
    min_price: float,
    max_price: float,
    paper: bool = False,
) -> None:
    """Update tracking for lowest and highest prices observed for this position."""
    table = "paper_positions" if paper else "positions"
    await db.execute(
        f"UPDATE {table} SET min_price_seen = ?, max_price_seen = ? WHERE position_id = ?",
        (min_price, max_price, position_id),
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
    now = int(time.time())

    await db.execute(
        f"""
        UPDATE {table}
        SET order_status = 'FILLED',
            closed_at = ?,
            exit_price = ?,
            realised_pnl = ?,
            close_reason = ?,
            resolution_delay = ? - (SELECT closes_at FROM markets WHERE markets.market_id = {table}.market_id)
        WHERE position_id = ?
        """,
        (now, exit_price, realised_pnl, close_reason, now, position_id),
    )
    await db.commit()


async def get_open_positions(
    db: aiosqlite.Connection, paper: bool = False
) -> List[Dict]:
    """Get all currently open or pending positions."""
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"SELECT * FROM {table} WHERE order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')"
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


async def get_last_traded_market_close_times(
    db: aiosqlite.Connection, paper: bool = False
) -> List[dict]:
    """
    Get the question, slug, and closes_at of all traded markets to find the last trade close time.
    """
    table = "paper_positions" if paper else "positions"
    cursor = await db.execute(
        f"""
        SELECT m.question, m.slug, m.closes_at
        FROM {table} p
        JOIN markets m ON p.market_id = m.market_id
        """
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]




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
    """Count currently open or pending positions whose markets are still active."""
    table = "paper_positions" if paper else "positions"
    now = int(time.time())
    cursor = await db.execute(
        f"""
        SELECT COUNT(*) FROM {table} p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')
          AND (m.closes_at IS NULL OR m.closes_at > ?)
        """,
        (now,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_total_exposure(
    db: aiosqlite.Connection, paper: bool = False
) -> float:
    """Sum of size_usdc for all OPEN or PENDING positions whose markets are still active."""
    table = "paper_positions" if paper else "positions"
    now = int(time.time())
    cursor = await db.execute(
        f"""
        SELECT COALESCE(SUM(p.size_usdc), 0.0) FROM {table} p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status IN ('OPEN', 'PENDING', 'PARTIAL_FILLED')
          AND (m.closes_at IS NULL OR m.closes_at > ?)
        """,
        (now,),
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
    # FIX: "UTC Midnight" Budget Crash
    # Use a strict 24-hour rolling window based on Unix time to bypass VPS clock drift / UTC issues.
    # Calculate NET PNL to prevent killing the bot if it is profitable.
    start_of_window = int(time.time()) - 86400

    cursor = await db.execute(
        f"""
        SELECT COALESCE(SUM(realised_pnl), 0.0)
        FROM {table}
        WHERE closed_at >= ?
        """,
        (start_of_window,),
    )
    row = await cursor.fetchone()
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
    
    # Losses (realised_pnl <= 0)
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl <= 0"
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
    cursor = await db.execute(
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
    await db.commit()
    return cursor.lastrowid


async def get_historical_yes_price(db: aiosqlite.Connection, market_id: str, seconds_ago: int = 15) -> Optional[float]:
    """
    Get the historical YES price (best_ask) for a market from spread_logs closest to N seconds ago.
    Falls back to the closest available historical tick if an exact match is not found.
    """
    target_ts = int(time.time()) - seconds_ago
    try:
        # 1. Try to find a tick within a tight +/- 5s window
        cursor = await db.execute(
            """
            SELECT best_ask FROM spread_logs 
            WHERE market_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY ABS(timestamp - ?) ASC 
            LIMIT 1
            """,
            (market_id, target_ts - 5, target_ts + 5, target_ts)
        )
        row = await cursor.fetchone()
        if row:
            return float(row[0])
        
        # 2. Fallback to the latest tick prior to target_ts
        cursor = await db.execute(
            """
            SELECT best_ask FROM spread_logs 
            WHERE market_id = ? AND timestamp <= ?
            ORDER BY timestamp DESC 
            LIMIT 1
            """,
            (market_id, target_ts)
        )
        row = await cursor.fetchone()
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
    cursor = await db.execute(
        """
        SELECT rolling_win_rate, kelly_fraction, wins, losses, total_trades, n_signals_resolved
        FROM win_rate_tracker
        WHERE symbol = ? AND window_size = ? AND direction = ?
        """,
        (symbol, str(window_size), direction),
    )
    row = await cursor.fetchone()
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
) -> int:
    """Log every signal evaluation (accepted or rejected) for guard auditing."""
    cursor = await db.execute(
        """
        INSERT INTO signals (
            market_id, symbol, window_size, direction, side,
            t1_market_id, t1_result, t2_market_id, t2_result,
            t3_market_id, t3_result, streak_depth, yes_price_at_signal,
            bid_ask_spread_at_signal, book_imbalance, cvd_60s, price_velocity,
            ev_computed, seconds_to_close, guard_result, guard_rejection_reason,
            signal_timestamp, outcome_source, candle_1_delta_bps, candle_2_delta_bps,
            entry_price, time_since_candle_close_secs, eventual_pnl,
            correlated_position_open, correlated_position_direction
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    await db.commit()
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
    cursor = await db.execute(
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
    await db.commit()
    return cursor.lastrowid


async def recalculate_win_rates(db: aiosqlite.Connection, rolling_n: int = 50) -> None:
    """
    Scans all evaluated signals in the signals table that have resolved,
    groups by bucket, recalculates rolling win rates, computes Half-Kelly fractions,
    and updates the win_rate_tracker.
    """
    db.row_factory = aiosqlite.Row
    # Query every detected market streak opportunity that has a known real-world resolution
    cursor = await db.execute(
        """
        SELECT signal_id, entry_price, side, eventual_resolution,
               symbol, window_size as window, direction, eventual_pnl,
               yes_price_at_signal, guard_result
        FROM signals
        WHERE eventual_resolution IS NOT NULL AND eventual_resolution != 'UNKNOWN'
        ORDER BY signal_timestamp ASC
        """
    )
    rows = await cursor.fetchall()
    
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
            
        # Compute real payout winner (YES resolved UP, or NO resolved DOWN)
        win = (side == "YES" and res == "UP") or (side == "NO" and res == "DOWN")
        pnl_val = (1.0 - entry_p) if win else -entry_p
        
        guard_res = str(row["guard_result"]).upper()
        traded = (guard_res == "ENTERED")
        
        buckets[key].append({
            "entry_price": entry_p,
            "realised_pnl": pnl_val,
            "side": side,
            "traded": traded
        })
        
    # 3. For each bucket, calculate rolling stats and Half-Kelly
    import time
    now_str = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
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
        await db.execute(
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
    await db.commit()


