"""
Unit tests for Phase 28 Telemetry Expansion Engine & Microstructure Math.
"""

import math
import os
import sqlite3
import pytest
from src.strategy.math_utils import calculate_bs_binary_prob
from scratch.apply_telemetry_migration_vps import apply_migration


def test_black_scholes_binary_prob_at_expiry():
    # Spot > Strike at expiry (ttl=0) -> P_BS should be 1.0
    assert calculate_bs_binary_prob(spot=65050.0, strike=65000.0, ttl_secs=0.0, vol_5m=0.001) == 1.0
    
    # Spot < Strike at expiry (ttl=0) -> P_BS should be 0.0
    assert calculate_bs_binary_prob(spot=64950.0, strike=65000.0, ttl_secs=0.0, vol_5m=0.001) == 0.0
    
    # Spot == Strike at expiry (ttl=0) -> P_BS should be 0.5
    assert calculate_bs_binary_prob(spot=65000.0, strike=65000.0, ttl_secs=0.0, vol_5m=0.001) == 0.5


def test_black_scholes_binary_prob_in_the_money():
    # ITM position with 120 seconds left -> prob should be > 0.5
    prob = calculate_bs_binary_prob(spot=65040.0, strike=65000.0, ttl_secs=120.0, vol_5m=0.001)
    assert 0.5 < prob <= 1.0


def test_black_scholes_binary_prob_out_of_the_money():
    # OTM position with 120 seconds left -> prob should be < 0.5
    prob = calculate_bs_binary_prob(spot=64960.0, strike=65000.0, ttl_secs=120.0, vol_5m=0.001)
    assert 0.0 <= prob < 0.5


def test_black_scholes_invalid_inputs():
    # Invalid spot or strike should return neutral fallback (0.5)
    assert calculate_bs_binary_prob(spot=-10.0, strike=65000.0, ttl_secs=100.0, vol_5m=0.001) == 0.5
    assert calculate_bs_binary_prob(spot=65000.0, strike=0.0, ttl_secs=100.0, vol_5m=0.001) == 0.5


def test_telemetry_migration_idempotency(tmp_path):
    db_file = str(tmp_path / "test_migration.db")
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE signals (
            signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            symbol TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE positions (
            position_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE paper_positions (
            position_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE profile_positions (
            position_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    # First run: should apply all columns
    res1 = apply_migration(db_file)
    assert res1 is True

    # Second run: should be idempotent and skip existing columns
    res2 = apply_migration(db_file)
    assert res2 is True

    # Verify column existence in signals
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(signals);")
    cols = [r[1] for r in cursor.fetchall()]
    assert "underlying_strike_price" in cols
    assert "underlying_spot_price" in cols
    assert "bs_implied_probability" in cols
    assert "depth_usd_1c" in cols
    assert "resolution_latency_seconds" in cols
    conn.close()


def test_decision_engine_imports_calculate_bs_binary_prob():
    from src.strategy.decision import calculate_bs_binary_prob as fn
    assert callable(fn)

