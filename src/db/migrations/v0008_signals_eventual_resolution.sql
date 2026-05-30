-- =============================================================================
-- Migration: v0008_signals_eventual_resolution
-- Adds eventual_resolution column to signals table to audit guard effectiveness.
-- =============================================================================

ALTER TABLE signals ADD COLUMN eventual_resolution TEXT;
