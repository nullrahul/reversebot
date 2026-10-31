import time
import pytest
import aiosqlite

from src.strategy.meta_logger import MetaStrategyLogger


@pytest.mark.asyncio
async def test_meta_logger_full_workflow(tmp_path):
    db_file = tmp_path / "test_meta.db"
    async with aiosqlite.connect(db_file) as db:
        db.row_factory = aiosqlite.Row
        
        # 1. Initialize schema
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profile_positions (
                position_id TEXT PRIMARY KEY,
                profile_name TEXT,
                market_id TEXT,
                symbol TEXT,
                window_minutes INTEGER,
                opened_at INTEGER,
                closed_at INTEGER,
                entry_price REAL,
                realised_pnl REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta_strategy_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                market_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                window_minutes INTEGER NOT NULL,
                opened_at INTEGER NOT NULL,
                seq_consecutive_wins INTEGER DEFAULT 0,
                seq_consecutive_losses INTEGER DEFAULT 0,
                seq_last_5_outcomes TEXT DEFAULT '',
                trailing_7d_total INTEGER DEFAULT 0,
                trailing_7d_wins INTEGER DEFAULT 0,
                trailing_7d_wr REAL DEFAULT 0.0,
                trailing_14d_total INTEGER DEFAULT 0,
                trailing_14d_wins INTEGER DEFAULT 0,
                trailing_14d_wr REAL DEFAULT 0.0,
                avg_entry_price_7d REAL DEFAULT 0.0,
                breakeven_wr_7d REAL DEFAULT 0.5,
                edge_over_breakeven_7d REAL DEFAULT 0.0,
                cross_profile_name TEXT DEFAULT '',
                cross_profile_7d_wr REAL DEFAULT 0.0,
                cross_profile_7d_total INTEGER DEFAULT 0,
                regime_signal TEXT DEFAULT 'UNKNOWN',
                hour_et INTEGER DEFAULT 0,
                day_of_week TEXT DEFAULT '',
                is_weekend INTEGER DEFAULT 0,
                closed_at INTEGER,
                realised_pnl REAL,
                trade_won INTEGER
            )
        """)
        await db.commit()

        now_ts = int(time.time())
        # Insert historical resolved trades for cheap_reversal (3 wins)
        for i in range(3):
            await db.execute("""
                INSERT INTO profile_positions (position_id, profile_name, market_id, symbol, window_minutes, opened_at, closed_at, entry_price, realised_pnl)
                VALUES (?, 'cheap_reversal', 'mkt_old', 'BTC', 5, ?, ?, 0.60, 5.0)
            """, (f"pos_old_cheap_{i}", now_ts - 3600 * (i + 1), now_ts - 3600 * i))
        
        # Insert historical resolved trades for opposing profile stop_loss (10 losses)
        for i in range(25):
            await db.execute("""
                INSERT INTO profile_positions (position_id, profile_name, market_id, symbol, window_minutes, opened_at, closed_at, entry_price, realised_pnl)
                VALUES (?, 'stop_loss', 'mkt_old_sl', 'BTC', 5, ?, ?, 0.40, -5.0)
            """, (f"pos_old_sl_{i}", now_ts - 3600 * (i + 1), now_ts - 3600 * i))
        
        await db.commit()

        # 2. Test log_trade_opened
        meta_logger = MetaStrategyLogger(db)
        pos_dict = {
            "position_id": "pos_new_123",
            "profile_name": "cheap_reversal",
            "market_id": "mkt_new_123",
            "symbol": "BTC",
            "window_minutes": 5,
            "opened_at": now_ts,
            "entry_price": 0.60,
            "hour_et": 14,
            "day_of_week": "Wednesday",
            "is_weekend": 0,
        }
        await meta_logger.log_trade_opened(pos_dict)

        # Verify record in meta_strategy_log
        cursor = await db.execute("SELECT * FROM meta_strategy_log WHERE position_id = 'pos_new_123'")
        row = await cursor.fetchone()
        assert row is not None
        assert row["profile_name"] == "cheap_reversal"
        assert row["seq_consecutive_wins"] == 3
        assert row["seq_consecutive_losses"] == 0
        assert row["seq_last_5_outcomes"] == "WWW"
        assert row["trailing_7d_total"] == 3
        assert row["trailing_7d_wins"] == 3
        assert row["trailing_7d_wr"] == 1.0
        assert row["cross_profile_name"] == "stop_loss"
        assert row["cross_profile_7d_total"] == 25
        assert row["cross_profile_7d_wr"] == 0.0
        assert row["regime_signal"] == "REVERSAL_FAVORED"

        # 3. Test log_trade_resolved
        await meta_logger.log_trade_resolved("pos_new_123", realised_pnl=12.50, closed_at=now_ts + 300)

        cursor = await db.execute("SELECT * FROM meta_strategy_log WHERE position_id = 'pos_new_123'")
        row_after = await cursor.fetchone()
        assert row_after["closed_at"] == now_ts + 300
        assert row_after["realised_pnl"] == 12.50
        assert row_after["trade_won"] == 1
