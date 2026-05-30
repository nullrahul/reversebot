-- =============================================================================
-- Migration: v0009_streak_audit_telemetry
-- Adds outcome_source, candle deltas, time_since_candle_close_secs, eventual_pnl
-- to signals table, and denormalized fields window_minutes, symbol to paper_positions
-- and positions.
-- =============================================================================

-- 1. Add new columns to signals
ALTER TABLE signals ADD COLUMN outcome_source TEXT DEFAULT 'GAMMA';
ALTER TABLE signals ADD COLUMN candle_1_delta_bps REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN candle_2_delta_bps REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN entry_price REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN time_since_candle_close_secs REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN eventual_pnl REAL DEFAULT 0.0;

-- 2. Add denormalized columns to paper_positions
ALTER TABLE paper_positions ADD COLUMN window_minutes INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN symbol TEXT DEFAULT '';

-- 3. Add denormalized columns to positions
ALTER TABLE positions ADD COLUMN window_minutes INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN symbol TEXT DEFAULT '';
