import aiosqlite
import json
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

class RejectionLogger:
    """
    Handles verbose logging of rejected signals into a dedicated SQLite database.
    This prevents bloating the main trade DB while providing deep audit trails.
    """
    def __init__(self, db_path: str = "data/rejections_rd.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._last_logged: Dict[tuple, float] = {}  # (market_id, primary_reason) -> timestamp

    async def init(self):
        """Initialize the rejections database and schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, timeout=60.0)
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 60000")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT,
                primary_reason TEXT,
                failed_checks_json TEXT,
                market_data_verbose_json TEXT,
                timestamp INTEGER
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_rejections_market ON rejections(market_id)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_rejections_time ON rejections(timestamp)")
        await self._db.commit()

    async def log_rejection(
        self, 
        market_id: str, 
        question: str, 
        primary_reason: str, 
        failed_checks: List[str], 
        market_data: Dict[str, Any]
    ):
        """Log a rejected signal with full verbose context, rate-limited to once every 5 minutes."""
        now = time.time()
        
        # Sanitize primary_reason by stripping numbers, percentages, and decimals
        # to prevent dynamic wiggles (prices, countdown seconds, levels) from bypassing the rate-limiter
        clean_reason = re.sub(r'-?\d+(?:\.\d+)?%?', '', primary_reason)
        clean_reason = re.sub(r'_+', '_', clean_reason)
        clean_reason = re.sub(r'\s+', ' ', clean_reason)
        clean_reason = clean_reason.strip('_ ').strip()
        
        cache_key = (market_id, clean_reason)
        
        # Rate-limiting: only write to DB if 5 minutes (300s) have passed since the last identical rejection
        last_time = self._last_logged.get(cache_key, 0.0)
        if now - last_time < 300.0:
            return  # Skip duplicate writing to prevent database bloat

        # Record this log attempt
        self._last_logged[cache_key] = now

        # Prune old cache entries to keep memory usage minimal (keeps under 1000 entries)
        if len(self._last_logged) > 1000:
            self._last_logged = {k: v for k, v in self._last_logged.items() if now - v < 300.0}

        if not self._db:
            await self.init()
            
        await self._db.execute(
            """
            INSERT INTO rejections (
                market_id, question, primary_reason, failed_checks_json, market_data_verbose_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                question,
                primary_reason,
                json.dumps(failed_checks),
                json.dumps(market_data),
                int(now)
            )
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
