-- =============================================================================
-- Migration: v0007_v3_missing_columns
-- Adds remaining quantitative audit fields to paper_positions and positions tables.
-- =============================================================================

-- 1. Add missing fields to paper_positions
ALTER TABLE paper_positions ADD COLUMN seconds_to_close_at_entry REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN market_age_at_entry_pct REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN hour_utc INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN hour_et INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN day_of_week TEXT DEFAULT '';
ALTER TABLE paper_positions ADD COLUMN is_weekend INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN session TEXT DEFAULT 'OFF';
ALTER TABLE paper_positions ADD COLUMN bankroll_at_entry REAL DEFAULT 0.0;

-- 2. Add complete set of V3 fields to positions (for live trading alignment)
ALTER TABLE positions ADD COLUMN win_prob_estimate REAL DEFAULT 0.5;
ALTER TABLE positions ADD COLUMN kelly_fraction_used REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN bid_ask_spread_at_entry REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN book_imbalance_at_entry REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN cvd_60s_at_entry REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN correlated_position_open INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN seconds_to_close_at_entry REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN market_age_at_entry_pct REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN hour_utc INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN hour_et INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN day_of_week TEXT DEFAULT '';
ALTER TABLE positions ADD COLUMN is_weekend INTEGER DEFAULT 0;
ALTER TABLE positions ADD COLUMN session TEXT DEFAULT 'OFF';
ALTER TABLE positions ADD COLUMN bankroll_at_entry REAL DEFAULT 0.0;
