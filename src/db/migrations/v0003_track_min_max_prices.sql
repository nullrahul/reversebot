-- v0003_track_min_max_prices.sql
-- Add max_price_seen and min_price_seen tracking columns to positions and paper_positions

ALTER TABLE positions ADD COLUMN max_price_seen REAL;
ALTER TABLE positions ADD COLUMN min_price_seen REAL;
ALTER TABLE paper_positions ADD COLUMN max_price_seen REAL;
ALTER TABLE paper_positions ADD COLUMN min_price_seen REAL;
