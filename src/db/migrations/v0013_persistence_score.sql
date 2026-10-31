-- Migration: v0013_persistence_score
-- Add persistence_score column to positions, paper_positions, and signals tables.

ALTER TABLE positions ADD COLUMN persistence_score REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN persistence_score REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN persistence_score REAL DEFAULT 0.0;
