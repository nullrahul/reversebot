-- Migration: v0014_add_strategy_mode
-- Add strategy_mode column to positions, paper_positions, and signals tables.

ALTER TABLE positions ADD COLUMN strategy_mode TEXT DEFAULT 'TREND';
ALTER TABLE paper_positions ADD COLUMN strategy_mode TEXT DEFAULT 'TREND';
ALTER TABLE signals ADD COLUMN strategy_mode TEXT DEFAULT 'TREND';
