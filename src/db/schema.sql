-- =============================================================================
-- Polymarket Reverse Bot — Database Schema
-- SQLite-compatible, PostgreSQL upgrade path maintained.
-- =============================================================================

-- Markets: every scanned market and its current state
CREATE TABLE IF NOT EXISTS markets (
    market_id           TEXT PRIMARY KEY,
    condition_id        TEXT,
    yes_token_id        TEXT,
    no_token_id         TEXT,
    question            TEXT NOT NULL,
    slug                TEXT,
    event_id            TEXT,
    category            TEXT,
    yes_price           REAL,       -- taker-side only, NO double-counting
    no_price            REAL,
    yes_volume_24h      REAL,       -- taker-side only
    total_volume        REAL,       -- taker-side only
    resolution_source   TEXT,
    closes_at           INTEGER,    -- unix timestamp UTC
    last_scanned_at     INTEGER,
    is_flagged          INTEGER DEFAULT 0,  -- 0 or 1
    flag_reason         TEXT,
    btc_price_at_market_open REAL,
    -- confirmation tracking
    last_seen_above_threshold INTEGER,  -- unix timestamp when first seen above threshold
    consecutive_cycles_above  INTEGER DEFAULT 0
);

-- Positions: every trade the bot opens or considers
CREATE TABLE IF NOT EXISTS positions (
    position_id         TEXT PRIMARY KEY,   -- uuid4
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    side                TEXT NOT NULL DEFAULT 'NO',  -- always "NO" for this strategy
    entry_price         REAL,
    size_usdc           REAL,              -- dollar amount risked
    shares_bought       REAL,              -- size_usdc / entry_price
    order_id            TEXT,              -- Polymarket CLOB order ID
    order_status        TEXT NOT NULL DEFAULT 'PENDING',
        -- PENDING/OPEN/FILLED/PARTIAL/CANCELLED/FAILED/CLOSE_FAILED
    opened_at           INTEGER,
    closed_at           INTEGER,           -- nullable
    exit_price          REAL,              -- nullable
    realised_pnl        REAL,              -- nullable
    close_reason        TEXT,
        -- TAKE_PROFIT/STOP_LOSS/RESOLUTION/MANUAL/KILL_SWITCH
    version_opened      TEXT,              -- bot version when opened
    notes               TEXT,
    max_price_seen      REAL,
    min_price_seen      REAL,
    resolution_delay    INTEGER,
    
    -- V3 Quantitative Fields
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
    
    -- Telemetry Denormalization Fields
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT ''
);

-- Partial unique index: prevent duplicate OPEN positions for the same market
-- This is the secondary guard (asyncio.Lock is primary)
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_market_open
    ON positions (market_id)
    WHERE order_status = 'OPEN';

-- Paper positions: mirror of positions for paper trading mode
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id         TEXT PRIMARY KEY,
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
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
    
    -- V3 Quantitative Fields
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
    
    -- Telemetry Denormalization Fields
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_market_open
    ON paper_positions (market_id)
    WHERE order_status = 'OPEN';

-- Win rate tracker: Maintains aggregate metrics for rolling sizing
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
    n_signals_resolved INTEGER DEFAULT 0,
    UNIQUE(symbol, window_size, direction)
);

-- Signals: every evaluated signal (accepted/rejected)
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
    signal_timestamp TEXT NOT NULL,
    eventual_resolution TEXT,
    
    -- Telemetry upgrades
    outcome_source TEXT DEFAULT 'GAMMA',
    candle_1_delta_bps REAL DEFAULT 0.0,
    candle_2_delta_bps REAL DEFAULT 0.0,
    entry_price REAL DEFAULT 0.0,
    time_since_candle_close_secs REAL DEFAULT 0.0,
    eventual_pnl REAL DEFAULT 0.0,
    correlated_position_open INTEGER DEFAULT 0,
    correlated_position_direction TEXT
);

-- Market regimes: snapshots aggregate market context every 60s
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

-- Scan log: full audit trail of every poll cycle
CREATE TABLE IF NOT EXISTS scan_log (
    scan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at          INTEGER NOT NULL,
    markets_scanned     INTEGER DEFAULT 0,
    markets_flagged     INTEGER DEFAULT 0,
    signals_fired       INTEGER DEFAULT 0,
    orders_placed       INTEGER DEFAULT 0,
    errors              TEXT,    -- nullable
    bot_version         TEXT NOT NULL
);

-- Events log: append-only audit log. NEVER UPDATE or DELETE rows here.
CREATE TABLE IF NOT EXISTS events_log (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time          INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
        -- SCAN/SIGNAL/ORDER_PLACED/FILL/CANCEL/KILL_SWITCH/
        -- WS_DISCONNECT/RATE_LIMIT/ERROR/VERSION_CHANGE
    market_id           TEXT,       -- nullable
    position_id         TEXT,       -- nullable
    payload             TEXT,       -- full JSON string of all relevant fields
    bot_version         TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
CREATE INDEX IF NOT EXISTS idx_markets_flagged ON markets(is_flagged);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(order_status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events_log(event_type);
CREATE INDEX IF NOT EXISTS idx_events_time ON events_log(event_time);
CREATE INDEX IF NOT EXISTS idx_scan_log_time ON scan_log(scanned_at);
