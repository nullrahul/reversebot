-- v0001_initial.sql
-- Initial schema creation for Polymarket Reverse Bot
-- Applied idempotently via IF NOT EXISTS clauses

-- This migration simply applies the full schema.
-- All CREATE statements use IF NOT EXISTS for idempotency.

-- Read and execute schema.sql contents
-- (In code, the migration runner reads schema.sql directly)

-- Migration metadata
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  INTEGER NOT NULL,
    description TEXT
);

-- Record this migration (only if not already applied)
INSERT OR IGNORE INTO schema_migrations (version, applied_at, description)
VALUES ('v0001', strftime('%s', 'now'), 'Initial schema creation');
