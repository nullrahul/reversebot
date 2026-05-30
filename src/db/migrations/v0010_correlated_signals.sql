-- =============================================================================
-- Migration: v0010_correlated_signals
-- Adds correlated_position_open and correlated_position_direction to signals,
-- and n_signals_resolved to win_rate_tracker
-- =============================================================================

ALTER TABLE signals ADD COLUMN correlated_position_open INTEGER DEFAULT 0;
ALTER TABLE signals ADD COLUMN correlated_position_direction TEXT;
ALTER TABLE win_rate_tracker ADD COLUMN n_signals_resolved INTEGER DEFAULT 0;
