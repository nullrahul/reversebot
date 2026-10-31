import aiosqlite
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import uuid
from src.logs.log_config import get_logger
from src.db.queries import get_db_lock, execute_with_retry, commit_with_retry, fetch_all, fetch_one, _DB_LOCKS

logger = get_logger("skip_db")

class LossMomentumSkipDb:
    """
    Handles logging of hypothetically skipped positions and their P&L auditing.
    """
    def __init__(self, db_path: str = "data/lossmomentumskip.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        """Initialize the lossmomentumskip database and schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, timeout=60.0)
        self._db.row_factory = aiosqlite.Row

        async with get_db_lock(self._db):
            # Enable WAL mode for concurrent read/write
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=60000")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS skipped_positions (
                    position_id TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    question TEXT,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    size_usdc REAL NOT NULL,
                    shares_bought REAL NOT NULL,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER,
                    exit_price REAL,
                    realised_pnl REAL,
                    close_reason TEXT,
                    trigger_position_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'SKIPPED',
                    symbol TEXT DEFAULT 'UNKNOWN'
                )
            """)
            try:
                await self._db.execute("ALTER TABLE skipped_positions ADD COLUMN symbol TEXT DEFAULT 'UNKNOWN'")
            except Exception:
                pass
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_skipped_market ON skipped_positions(market_id)")
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_skipped_trigger ON skipped_positions(trigger_position_id)")
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_skipped_status ON skipped_positions(status)")
            await commit_with_retry(self._db)
        logger.info("lossmomentumskip_db_initialized", db_path=self.db_path)

    async def record_skip(
        self,
        market_id: str,
        question: str,
        side: str,
        entry_price: float,
        size_usdc: float,
        shares_bought: float,
        trigger_position_id: str,
        symbol: str,
    ) -> str:
        """Record a new skipped/hypothetical position."""
        if not self._db:
            await self.init()
        position_id = str(uuid.uuid4())
        opened_at = int(time.time())
        async with get_db_lock(self._db):
            await execute_with_retry(
                self._db,
                """
                INSERT INTO skipped_positions (
                    position_id, market_id, question, side, entry_price,
                    size_usdc, shares_bought, opened_at, trigger_position_id, status, symbol
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SKIPPED', ?)
                """,
                (
                    position_id,
                    market_id,
                    question,
                    side,
                    entry_price,
                    size_usdc,
                    shares_bought,
                    opened_at,
                    trigger_position_id,
                    symbol,
                )
            )
            await commit_with_retry(self._db)
        return position_id

    async def has_skipped_for_trigger(self, trigger_position_id: str) -> bool:
        """Check if we already recorded a skip for the given real loss position."""
        if not self._db:
            await self.init()
        row = await fetch_one(
            self._db,
            "SELECT 1 FROM skipped_positions WHERE trigger_position_id = ? LIMIT 1",
            (trigger_position_id,)
        )
        return row is not None

    async def get_unresolved_skips(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all skipped positions that are still in SKIPPED status."""
        if not self._db:
            await self.init()
        query = "SELECT * FROM skipped_positions WHERE status = 'SKIPPED'"
        rows = await fetch_all(self._db, query)
        return [dict(r) for r in rows]

    async def resolve_skip(
        self,
        position_id: str,
        exit_price: float,
        realised_pnl: float,
        close_reason: str = "RESOLUTION",
        closed_at: Optional[int] = None
    ):
        """Update a skipped position with resolution data."""
        if not self._db:
            await self.init()
        if closed_at is None:
            closed_at = int(time.time())
        async with get_db_lock(self._db):
            await execute_with_retry(
                self._db,
                """
                UPDATE skipped_positions
                SET status = 'RESOLVED',
                    closed_at = ?,
                    exit_price = ?,
                    realised_pnl = ?,
                    close_reason = ?
                WHERE position_id = ?
                """,
                (closed_at, exit_price, realised_pnl, close_reason, position_id)
            )
            await commit_with_retry(self._db)

    async def get_last_closed_skipped_position(self, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the most recently resolved skipped position."""
        if not self._db:
            await self.init()
        query = (
            "SELECT position_id, realised_pnl, closed_at FROM skipped_positions "
            "WHERE status = 'RESOLVED' AND realised_pnl IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 1"
        )
        row = await fetch_one(self._db, query)
        return dict(row) if row else None

    async def close(self):
        """Close the database connection."""
        if self._db:
            _DB_LOCKS.pop(id(self._db), None)
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None
