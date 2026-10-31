-- =============================================================================
-- Migration v0012: Consolidated Microstructure & Telemetry Expansion Engine
-- Compatible with SQLite non-blocking ALTER TABLE updates.
-- =============================================================================

-- 1. Underlying Asset Telemetry (Binance Spot)
ALTER TABLE signals ADD COLUMN underlying_strike_price REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN underlying_spot_price REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN underlying_distance_bps REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN underlying_spot_velocity_10s REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN underlying_realized_vol_5m REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN underlying_realized_vol_1h REAL DEFAULT 0.0;

-- 2. Microstructure & Orderbook Gradient Telemetry
ALTER TABLE signals ADD COLUMN sim_slippage_bps_at_target REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN depth_usd_1c REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN depth_usd_5c REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN book_imbalance_momentum_10s REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN ask_depth_usd_1c REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN bid_depth_usd_1c REAL DEFAULT 0.0;

-- 3. Binary Option Engine & Pricing Edge
ALTER TABLE signals ADD COLUMN bs_implied_probability REAL DEFAULT 0.5;
ALTER TABLE signals ADD COLUMN option_pricing_edge_bps REAL DEFAULT 0.0;

-- 4. Execution & Latency Telemetry
ALTER TABLE signals ADD COLUMN execution_attempts INTEGER DEFAULT 1;
ALTER TABLE signals ADD COLUMN execution_duration_ms REAL DEFAULT 0.0;
ALTER TABLE signals ADD COLUMN fill_percentage REAL DEFAULT 1.0;
ALTER TABLE signals ADD COLUMN resolution_latency_seconds INTEGER DEFAULT 0;

-- Mirror critical telemetry across active position tables
ALTER TABLE positions ADD COLUMN underlying_distance_bps REAL DEFAULT 0.0;
ALTER TABLE positions ADD COLUMN bs_implied_probability REAL DEFAULT 0.5;
ALTER TABLE positions ADD COLUMN resolution_latency_seconds INTEGER DEFAULT 0;

ALTER TABLE paper_positions ADD COLUMN underlying_distance_bps REAL DEFAULT 0.0;
ALTER TABLE paper_positions ADD COLUMN bs_implied_probability REAL DEFAULT 0.5;
ALTER TABLE paper_positions ADD COLUMN resolution_latency_seconds INTEGER DEFAULT 0;

ALTER TABLE profile_positions ADD COLUMN underlying_distance_bps REAL DEFAULT 0.0;
ALTER TABLE profile_positions ADD COLUMN bs_implied_probability REAL DEFAULT 0.5;
ALTER TABLE profile_positions ADD COLUMN resolution_latency_seconds INTEGER DEFAULT 0;
