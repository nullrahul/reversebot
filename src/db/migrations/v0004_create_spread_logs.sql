-- =============================================================================
-- Migration: v0004_create_spread_logs
-- Creates the spread_logs table to collect actual historical Polymarket
-- bid/ask spreads tick-by-tick for quant optimization analysis.
-- =============================================================================

CREATE TABLE IF NOT EXISTS spread_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           INTEGER NOT NULL,   -- unix timestamp UTC
    market_id           TEXT NOT NULL,
    symbol              TEXT NOT NULL,      -- BTC or ETH
    window_minutes      INTEGER NOT NULL,   -- 5 or 15
    best_bid            REAL,
    best_ask            REAL,
    spread_amount       REAL,
    spread_pct          REAL
);

-- Index for fast quant queries on asset, timeframe, and time window
CREATE INDEX IF NOT EXISTS idx_spread_logs_market ON spread_logs(market_id);
CREATE INDEX IF NOT EXISTS idx_spread_logs_symbol_window ON spread_logs(symbol, window_minutes);
CREATE INDEX IF NOT EXISTS idx_spread_logs_time ON spread_logs(timestamp);
