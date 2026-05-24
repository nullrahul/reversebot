-- v0004_add_resolution_delay.sql
-- Add resolution_delay tracking column to positions and paper_positions

ALTER TABLE positions ADD COLUMN resolution_delay INTEGER;
ALTER TABLE paper_positions ADD COLUMN resolution_delay INTEGER;
