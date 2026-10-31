-- Migration v0016: Create wick_events table to record wicks below 0.50
CREATE TABLE IF NOT EXISTS wick_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         TEXT NOT NULL,
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    profile_name        TEXT NOT NULL,
    start_time          REAL NOT NULL,      -- Unix timestamp (float)
    end_time            REAL NOT NULL,      -- Unix timestamp (float)
    duration_ms         REAL NOT NULL,      -- Duration of the dip in milliseconds
    resolved_by         TEXT NOT NULL,      -- 'RECOVERY' or 'STOP_LOSS'
    entry_price         REAL NOT NULL,
    trigger_price       REAL NOT NULL,      -- Price at the start of dip
    exit_price          REAL,               -- Final price if stopped out or recovered
    timestamp           INTEGER NOT NULL    -- Log timestamp (seconds)
);

CREATE INDEX IF NOT EXISTS idx_wick_events_position ON wick_events(position_id);
CREATE INDEX IF NOT EXISTS idx_wick_events_market ON wick_events(market_id);
CREATE INDEX IF NOT EXISTS idx_wick_events_profile ON wick_events(profile_name);
