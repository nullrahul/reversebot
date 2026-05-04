import aiosqlite
import json
import time
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

    async def init(self):
        """Initialize the rejections database and schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        
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
        """Log a rejected signal with full verbose context."""
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
                int(time.time())
            )
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
