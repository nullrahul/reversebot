-- Migration v0015: Create profile_positions table for Strategy Profile Framework
CREATE TABLE IF NOT EXISTS profile_positions (
    position_id         TEXT PRIMARY KEY,   -- uuid4
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
    symbol                    TEXT DEFAULT '',
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    
    -- Profile identifier
    profile_name              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_positions_name ON profile_positions(profile_name);
CREATE INDEX IF NOT EXISTS idx_profile_positions_status ON profile_positions(order_status);
CREATE INDEX IF NOT EXISTS idx_profile_positions_market ON profile_positions(market_id);
