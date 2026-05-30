-- =============================================================================
-- Migration: v0005_add_fok_simulation_columns
-- Adds FOK order book sweep simulation and live API latency tracking columns
-- to the spread_logs table.
-- =============================================================================

ALTER TABLE spread_logs ADD COLUMN sim_fok_yes_price REAL DEFAULT 0.0;
ALTER TABLE spread_logs ADD COLUMN sim_fok_yes_slippage REAL DEFAULT 0.0;
ALTER TABLE spread_logs ADD COLUMN sim_fok_yes_fillable INTEGER DEFAULT 0;
ALTER TABLE spread_logs ADD COLUMN sim_fok_no_price REAL DEFAULT 0.0;
ALTER TABLE spread_logs ADD COLUMN sim_fok_no_slippage REAL DEFAULT 0.0;
ALTER TABLE spread_logs ADD COLUMN sim_fok_no_fillable INTEGER DEFAULT 0;
ALTER TABLE spread_logs ADD COLUMN api_latency_ms REAL DEFAULT 0.0;
