import os
import shutil
import pytest
import asyncio
import sqlite3
from src.db.rejections import RejectionLogger

DB_PATH = "data/test_rejections.db"

@pytest.fixture
def clean_db():
    # Remove test DB if it exists
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    yield
    # Cleanup after test
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    # Also clean up parent folder if empty
    db_dir = os.path.dirname(DB_PATH)
    if os.path.exists(db_dir) and not os.listdir(db_dir):
        os.rmdir(db_dir)

@pytest.mark.asyncio
async def test_rejection_logger_rate_limiting_and_sanitization(clean_db):
    logger = RejectionLogger(db_path=DB_PATH)
    await logger.init()

    market_id = "test_market_1"
    question = "Is Bitcoin going up?"
    failed_checks = ["yes_exhaustion_gate"]
    market_data = {"yes_price": 0.53, "no_price": 0.47}

    # 1. First logging attempt
    await logger.log_rejection(
        market_id=market_id,
        question=question,
        primary_reason="YES_EXHAUSTION_ALREADY_PRICED_0.5300_LIMIT_0.52",
        failed_checks=failed_checks,
        market_data=market_data
    )

    # Verify database has exactly 1 entry
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM rejections")
    assert cursor.fetchone()[0] == 1

    # Verify contents of the first entry are preserved exactly as passed
    cursor.execute("SELECT primary_reason, market_id, question FROM rejections")
    row = cursor.fetchone()
    assert row[0] == "YES_EXHAUSTION_ALREADY_PRICED_0.5300_LIMIT_0.52"
    assert row[1] == market_id
    assert row[2] == question
    conn.close()

    # 2. Second logging attempt with slight wiggle (different price)
    # With the fix, this should be rate-limited and NOT written to the DB.
    await logger.log_rejection(
        market_id=market_id,
        question=question,
        primary_reason="YES_EXHAUSTION_ALREADY_PRICED_0.5400_LIMIT_0.52",
        failed_checks=failed_checks,
        market_data=market_data
    )

    # Verify database STILL has exactly 1 entry (rate limit succeeded)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM rejections")
    assert cursor.fetchone()[0] == 1
    conn.close()

    # 3. Third logging attempt with completely different reason
    # This should be written to the DB.
    await logger.log_rejection(
        market_id=market_id,
        question=question,
        primary_reason="FALLING_KNIFE_YES_APPROACH_-0.0350",
        failed_checks=["price_approach_direction_gate"],
        market_data=market_data
    )

    # Verify database now has 2 entries
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM rejections")
    assert cursor.fetchone()[0] == 2

    # Verify the second entry matches the new reason
    cursor.execute("SELECT primary_reason FROM rejections ORDER BY id DESC LIMIT 1")
    assert cursor.fetchone()[0] == "FALLING_KNIFE_YES_APPROACH_-0.0350"
    conn.close()

    await logger.close()
