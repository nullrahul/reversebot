-- =============================================================================
-- Migration: v0011_btc_price_at_market_open
-- Adds btc_price_at_market_open to markets table
-- =============================================================================

ALTER TABLE markets ADD COLUMN btc_price_at_market_open REAL;
