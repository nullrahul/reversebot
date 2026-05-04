-- v0002_add_token_ids.sql
-- Add unique market tracking columns to the markets table

ALTER TABLE markets ADD COLUMN condition_id TEXT;
ALTER TABLE markets ADD COLUMN yes_token_id TEXT;
ALTER TABLE markets ADD COLUMN no_token_id TEXT;
ALTER TABLE markets ADD COLUMN slug TEXT;
ALTER TABLE markets ADD COLUMN event_id TEXT;
