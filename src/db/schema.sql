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
    notes               TEXT
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
    notes               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_market_open
    ON paper_positions (market_id)
    WHERE order_status = 'OPEN';

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
