-- File: ticks_YYYY-MM-DD.db (daily rotated tick database)

-- Core table: Polymarket contract snapshots every 2 seconds
CREATE TABLE IF NOT EXISTS market_snapshots (
    tick_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms        INTEGER NOT NULL,
    market_id           TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    window_minutes      INTEGER NOT NULL,
    closes_at           INTEGER NOT NULL,
    
    -- Polymarket contract prices
    yes_price           REAL,
    no_price            REAL,
    yes_best_bid        REAL,
    yes_best_ask        REAL,
    no_best_bid         REAL,
    no_best_ask         REAL,
    bid_ask_spread      REAL,
    
    -- Orderbook depth & flow
    book_imbalance      REAL,
    depth_usd_1c        REAL,
    depth_usd_5c        REAL,
    ask_depth_usd_1c    REAL,
    bid_depth_usd_1c    REAL,
    cvd_60s             REAL,
    cvd_5m              REAL,
    
    -- Streak & regime state
    streak_depth        INTEGER,
    streak_quality      REAL,
    persistence_score   REAL,
    strategy_mode       TEXT,
    
    -- Underlying crypto
    spot_price          REAL,
    spot_velocity_10s   REAL,
    spot_velocity_z     REAL,
    distance_bps        REAL,
    realized_vol_5m     REAL,
    realized_vol_1h     REAL,
    
    -- Timing
    seconds_remaining   REAL,
    market_age_pct      REAL,
    
    -- Derived analytics
    bs_implied_prob     REAL,
    ev_at_snapshot      REAL
);

CREATE INDEX IF NOT EXISTS idx_ms_market_ts ON market_snapshots(market_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_ms_symbol_ts ON market_snapshots(symbol, timestamp_ms);

-- External: Binance spot & telemetry data for BTC, ETH, SOL, XRP
CREATE TABLE IF NOT EXISTS external_ticks (
    tick_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms        INTEGER NOT NULL,
    symbol              TEXT NOT NULL,
    spot_price          REAL,
    cvd_60s             REAL,
    cvd_5m              REAL,
    spot_velocity_10s   REAL,
    spot_velocity_z     REAL,
    realized_vol_5m     REAL,
    realized_vol_1h     REAL
);

CREATE INDEX IF NOT EXISTS idx_ext_symbol_ts ON external_ticks(symbol, timestamp_ms);

-- Market lifecycle events (open, close, resolve)
CREATE TABLE IF NOT EXISTS market_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms        INTEGER NOT NULL,
    market_id           TEXT NOT NULL,
    event_type          TEXT NOT NULL,  -- DISCOVERED / EXPIRED / RESOLVED
    symbol              TEXT NOT NULL,
    window_minutes      INTEGER NOT NULL,
    closes_at           INTEGER,
    resolution_result   TEXT,           -- UP / DOWN
    yes_price_at_event  REAL,
    no_price_at_event   REAL
);

-- Recording session metadata
CREATE TABLE IF NOT EXISTS recording_sessions (
    session_id          TEXT PRIMARY KEY,
    started_at          INTEGER NOT NULL,
    stopped_at          INTEGER,
    interval_seconds    REAL NOT NULL,
    bot_version         TEXT,
    markets_recorded    INTEGER DEFAULT 0,
    ticks_recorded      INTEGER DEFAULT 0,
    gaps_detected       INTEGER DEFAULT 0,
    notes               TEXT
);

-- Data gaps (when bot was down or snapshot was stale)
CREATE TABLE IF NOT EXISTS data_gaps (
    gap_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_start_ms        INTEGER NOT NULL,
    gap_end_ms          INTEGER,
    reason              TEXT  -- BOT_DOWN / STALE_SNAPSHOT / RECORDER_RESTART
);
