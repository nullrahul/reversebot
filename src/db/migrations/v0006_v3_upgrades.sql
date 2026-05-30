-- =============================================================================
-- Migration: v0006_v3_upgrades
-- Adds quantitative analysis fields to paper_positions and creates signals,
-- win_rate_tracker, and market_regimes tables.
-- =============================================================================

-- 1. Alter paper_positions table to add quantitative audit fields
ALTER TABLE paper_positions ADD COLUMN win_prob_estimate REAL DEFAULT 0.5;
ALTER TABLE paper_positions ADD COLUMN kelly_fraction_used REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN bid_ask_spread_at_entry REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN book_imbalance_at_entry REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN cvd_60s_at_entry REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN correlated_position_open INTEGER DEFAULT 0;

-- 2. Create win_rate_tracker table
CREATE TABLE IF NOT EXISTS win_rate_tracker (
    tracker_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    window_size TEXT NOT NULL,
    direction TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    rolling_n INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    rolling_win_rate REAL DEFAULT 0.5,
    kelly_fraction REAL DEFAULT 0.0,
    avg_ev REAL DEFAULT 0.0,
    avg_pnl REAL DEFAULT 0.0,
    sharpe_ratio REAL DEFAULT 0.0,
    last_updated TEXT NOT NULL,
    staleness_flag INTEGER DEFAULT 0,
    UNIQUE(symbol, window_size, direction)
);

-- 3. Create signals table to log every evaluated signal (accepted/rejected)
CREATE TABLE IF NOT EXISTS signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    window_size TEXT NOT NULL,
    direction TEXT NOT NULL,
    side TEXT NOT NULL,
    t1_market_id TEXT,
    t1_result TEXT,
    t2_market_id TEXT,
    t2_result TEXT,
    t3_market_id TEXT,
    t3_result TEXT,
    streak_depth INTEGER DEFAULT 2,
    yes_price_at_signal REAL DEFAULT 0.0,
    bid_ask_spread_at_signal REAL DEFAULT 0.0,
    book_imbalance REAL DEFAULT 0.0,
    cvd_60s REAL DEFAULT 0.0,
    price_velocity REAL DEFAULT 0.0,
    ev_computed REAL DEFAULT 0.0,
    seconds_to_close REAL DEFAULT 0.0,
    guard_result TEXT NOT NULL, -- 'ENTERED' or 'REJECTED'
    guard_rejection_reason TEXT,
    signal_timestamp TEXT NOT NULL
);

-- 4. Create market_regimes table to snapshot aggregate market context every 60s
CREATE TABLE IF NOT EXISTS market_regimes (
    regime_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    btc_binance_price REAL DEFAULT 0.0,
    eth_binance_price REAL DEFAULT 0.0,
    cvd_60s_btc REAL DEFAULT 0.0,
    cvd_60s_eth REAL DEFAULT 0.0,
    cvd_5m_btc REAL DEFAULT 0.0,
    cvd_5m_eth REAL DEFAULT 0.0,
    rolling_spread_median REAL DEFAULT 0.0,
    spread_regime_multiplier REAL DEFAULT 1.0,
    implied_vol_proxy REAL DEFAULT 1.0,
    regime_label TEXT DEFAULT 'NORMAL',
    session TEXT DEFAULT 'OFF'
);
