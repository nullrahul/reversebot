-- =============================================================================
-- Polymarket Reverse Bot — Database Schema
-- SQLite-compatible, PostgreSQL upgrade path maintained.
-- =============================================================================

-- Markets: every scanned market and its current state
CREATE TABLE IF NOT EXISTS markets (
    market_id           TEXT PRIMARY KEY,
    condition_id        TEXT,
    yes_token_id        TEXT,
    no_token_id         TEXT,
    question            TEXT NOT NULL,
    slug                TEXT,
    event_id            TEXT,
    category            TEXT,
    yes_price           REAL,       -- taker-side only, NO double-counting
    no_price            REAL,
    yes_volume_24h      REAL,       -- taker-side only
    total_volume        REAL,       -- taker-side only
    resolution_source   TEXT,
    closes_at           INTEGER,    -- unix timestamp UTC
    last_scanned_at     INTEGER,
    is_flagged          INTEGER DEFAULT 0,  -- 0 or 1
    flag_reason         TEXT,
    btc_price_at_market_open REAL,
    -- confirmation tracking
    last_seen_above_threshold INTEGER,  -- unix timestamp when first seen above threshold
    consecutive_cycles_above  INTEGER DEFAULT 0
);

-- Positions: every trade the bot opens or considers
CREATE TABLE IF NOT EXISTS positions (
    position_id         TEXT PRIMARY KEY,   -- uuid4
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    side                TEXT NOT NULL DEFAULT 'NO',  -- always "NO" for this strategy
    entry_price         REAL,
    size_usdc           REAL,              -- dollar amount risked
    shares_bought       REAL,              -- size_usdc / entry_price
    order_id            TEXT,              -- Polymarket CLOB order ID
    order_status        TEXT NOT NULL DEFAULT 'PENDING',
        -- PENDING/OPEN/FILLED/PARTIAL/CANCELLED/FAILED/CLOSE_FAILED
    opened_at           INTEGER,
    closed_at           INTEGER,           -- nullable
    exit_price          REAL,              -- nullable
    realised_pnl        REAL,              -- nullable
    close_reason        TEXT,
        -- TAKE_PROFIT/STOP_LOSS/RESOLUTION/MANUAL/KILL_SWITCH
    version_opened      TEXT,              -- bot version when opened
    notes               TEXT,
    max_price_seen      REAL,
    min_price_seen      REAL,
    resolution_delay    INTEGER,
    
    -- V3 Quantitative Fields
    win_prob_estimate         REAL DEFAULT 0.5,
    kelly_fraction_used       REAL DEFAULT 0.0,
    bid_ask_spread_at_entry   REAL DEFAULT 0.0,
    book_imbalance_at_entry   REAL DEFAULT 0.0,
    cvd_60s_at_entry          REAL DEFAULT 0.0,
    correlated_position_open  INTEGER DEFAULT 0,
    seconds_to_close_at_entry REAL DEFAULT 0.0,
    market_age_at_entry_pct   REAL DEFAULT 0.0,
    hour_utc                  INTEGER DEFAULT 0,
    hour_et                   INTEGER DEFAULT 0,
    day_of_week               TEXT DEFAULT '',
    is_weekend                INTEGER DEFAULT 0,
    session                   TEXT DEFAULT 'OFF',
    bankroll_at_entry         REAL DEFAULT 0.0,
    
    -- Telemetry Denormalization Fields
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT '',
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    underlying_distance_bps   REAL DEFAULT 0.0,
    bs_implied_probability    REAL DEFAULT 0.5,
    resolution_latency_seconds INTEGER DEFAULT 0
);

-- Partial unique index: prevent duplicate OPEN positions for the same market
-- This is the secondary guard (asyncio.Lock is primary)
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_market_open
    ON positions (market_id)
    WHERE order_status = 'OPEN';

-- Paper positions: mirror of positions for paper trading mode
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id         TEXT PRIMARY KEY,
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    side                TEXT NOT NULL DEFAULT 'NO',
    entry_price         REAL,
    size_usdc           REAL,
    shares_bought       REAL,
    order_id            TEXT,
    order_status        TEXT NOT NULL DEFAULT 'PENDING',
    opened_at           INTEGER,
    closed_at           INTEGER,
    exit_price          REAL,
    realised_pnl        REAL,
    close_reason        TEXT,
    version_opened      TEXT,
    notes               TEXT,
    max_price_seen      REAL,
    min_price_seen      REAL,
    resolution_delay    INTEGER,
    
    -- V3 Quantitative Fields
    win_prob_estimate         REAL DEFAULT 0.5,
    kelly_fraction_used       REAL DEFAULT 0.0,
    bid_ask_spread_at_entry   REAL DEFAULT 0.0,
    book_imbalance_at_entry   REAL DEFAULT 0.0,
    cvd_60s_at_entry          REAL DEFAULT 0.0,
    correlated_position_open  INTEGER DEFAULT 0,
    seconds_to_close_at_entry REAL DEFAULT 0.0,
    market_age_at_entry_pct   REAL DEFAULT 0.0,
    hour_utc                  INTEGER DEFAULT 0,
    hour_et                   INTEGER DEFAULT 0,
    day_of_week               TEXT DEFAULT '',
    is_weekend                INTEGER DEFAULT 0,
    session                   TEXT DEFAULT 'OFF',
    bankroll_at_entry         REAL DEFAULT 0.0,
    
    -- Telemetry Denormalization Fields
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT '',
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    underlying_distance_bps   REAL DEFAULT 0.0,
    bs_implied_probability    REAL DEFAULT 0.5,
    resolution_latency_seconds INTEGER DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_market_open
    ON paper_positions (market_id)
    WHERE order_status = 'OPEN';

-- Profile positions: virtual simulation positions for comparing strategies
CREATE TABLE IF NOT EXISTS profile_positions (
    position_id         TEXT PRIMARY KEY,   -- uuid4
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    side                TEXT NOT NULL DEFAULT 'NO',
    entry_price         REAL,
    size_usdc           REAL,
    shares_bought       REAL,
    order_id            TEXT,
    order_status        TEXT NOT NULL DEFAULT 'PENDING',
    opened_at           INTEGER,
    closed_at           INTEGER,
    exit_price          REAL,
    realised_pnl        REAL,
    close_reason        TEXT,
    version_opened      TEXT,
    notes               TEXT,
    max_price_seen      REAL,
    min_price_seen      REAL,
    resolution_delay    INTEGER,
    
    -- V3 Quantitative Fields
    win_prob_estimate         REAL DEFAULT 0.5,
    kelly_fraction_used       REAL DEFAULT 0.0,
    bid_ask_spread_at_entry   REAL DEFAULT 0.0,
    book_imbalance_at_entry   REAL DEFAULT 0.0,
    cvd_60s_at_entry          REAL DEFAULT 0.0,
    correlated_position_open  INTEGER DEFAULT 0,
    seconds_to_close_at_entry REAL DEFAULT 0.0,
    market_age_at_entry_pct   REAL DEFAULT 0.0,
    hour_utc                  INTEGER DEFAULT 0,
    hour_et                   INTEGER DEFAULT 0,
    day_of_week               TEXT DEFAULT '',
    is_weekend                INTEGER DEFAULT 0,
    session                   TEXT DEFAULT 'OFF',
    bankroll_at_entry         REAL DEFAULT 0.0,
    
    -- Telemetry Denormalization Fields
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT '',
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    underlying_distance_bps   REAL DEFAULT 0.0,
    bs_implied_probability    REAL DEFAULT 0.5,
    resolution_latency_seconds INTEGER DEFAULT 0,
    realized_vol_5m           REAL DEFAULT 0.0,
    
    -- Profile identifier
    profile_name              TEXT NOT NULL
);

-- Executioner positions: dedicated tracking table for combined executioner profile
CREATE TABLE IF NOT EXISTS executioner_positions (
    position_id         TEXT PRIMARY KEY,
    base_profile_name   TEXT NOT NULL,
    request_id          INTEGER NOT NULL,
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    side                TEXT NOT NULL DEFAULT 'NO',
    entry_price         REAL,
    size_usdc           REAL,
    shares_bought       REAL,
    order_id            TEXT,
    order_status        TEXT NOT NULL DEFAULT 'PENDING',
    opened_at           INTEGER,
    closed_at           INTEGER,
    exit_price          REAL,
    realised_pnl        REAL,
    close_reason        TEXT,
    version_opened      TEXT,
    notes               TEXT,
    max_price_seen      REAL,
    min_price_seen      REAL,
    resolution_delay    INTEGER,
    win_prob_estimate         REAL DEFAULT 0.5,
    kelly_fraction_used       REAL DEFAULT 0.0,
    bid_ask_spread_at_entry   REAL DEFAULT 0.0,
    book_imbalance_at_entry   REAL DEFAULT 0.0,
    cvd_60s_at_entry          REAL DEFAULT 0.0,
    correlated_position_open  INTEGER DEFAULT 0,
    seconds_to_close_at_entry REAL DEFAULT 0.0,
    market_age_at_entry_pct   REAL DEFAULT 0.0,
    hour_utc                  INTEGER DEFAULT 0,
    hour_et                   INTEGER DEFAULT 0,
    day_of_week               TEXT DEFAULT '',
    is_weekend                INTEGER DEFAULT 0,
    session                   TEXT DEFAULT 'OFF',
    bankroll_at_entry         REAL DEFAULT 0.0,
    window_minutes            INTEGER DEFAULT 0,
    symbol                    TEXT DEFAULT '',
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    underlying_distance_bps   REAL DEFAULT 0.0,
    bs_implied_probability    REAL DEFAULT 0.5,
    resolution_latency_seconds INTEGER DEFAULT 0,
    realized_vol_5m           REAL DEFAULT 0.0,
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profile_positions_name ON profile_positions(profile_name);
CREATE INDEX IF NOT EXISTS idx_profile_positions_status ON profile_positions(order_status);
CREATE INDEX IF NOT EXISTS idx_profile_positions_market ON profile_positions(market_id);


-- Win rate tracker: Maintains aggregate metrics for rolling sizing
CREATE TABLE IF NOT EXISTS win_rate_tracker (
    tracker_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    window_size TEXT NOT NULL,
    direction TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    rolling_n INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    rolling_win_rate REAL DEFAULT 0.5,
    kelly_fraction REAL DEFAULT 0.0,
    avg_ev REAL DEFAULT 0.0,
    avg_pnl REAL DEFAULT 0.0,
    sharpe_ratio REAL DEFAULT 0.0,
    last_updated TEXT NOT NULL,
    staleness_flag INTEGER DEFAULT 0,
    n_signals_resolved INTEGER DEFAULT 0,
    UNIQUE(symbol, window_size, direction)
);

-- Signals: every evaluated signal (accepted/rejected)
CREATE TABLE IF NOT EXISTS signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    window_size TEXT NOT NULL,
    direction TEXT NOT NULL,
    side TEXT NOT NULL,
    t1_market_id TEXT,
    t1_result TEXT,
    t2_market_id TEXT,
    t2_result TEXT,
    t3_market_id TEXT,
    t3_result TEXT,
    streak_depth INTEGER DEFAULT 2,
    yes_price_at_signal REAL DEFAULT 0.0,
    bid_ask_spread_at_signal REAL DEFAULT 0.0,
    book_imbalance REAL DEFAULT 0.0,
    cvd_60s REAL DEFAULT 0.0,
    price_velocity REAL DEFAULT 0.0,
    ev_computed REAL DEFAULT 0.0,
    seconds_to_close REAL DEFAULT 0.0,
    guard_result TEXT NOT NULL, -- 'ENTERED' or 'REJECTED'
    guard_rejection_reason TEXT,
    signal_timestamp TEXT NOT NULL,
    eventual_resolution TEXT,
    
    -- Telemetry upgrades
    outcome_source TEXT DEFAULT 'GAMMA',
    candle_1_delta_bps REAL DEFAULT 0.0,
    candle_2_delta_bps REAL DEFAULT 0.0,
    entry_price REAL DEFAULT 0.0,
    time_since_candle_close_secs REAL DEFAULT 0.0,
    eventual_pnl REAL DEFAULT 0.0,
    correlated_position_open INTEGER DEFAULT 0,
    correlated_position_direction TEXT,
    persistence_score         REAL DEFAULT 0.0,
    strategy_mode             TEXT DEFAULT 'TREND',
    
    -- Phase 28 Telemetry Expansion Engine Fields
    underlying_strike_price       REAL DEFAULT 0.0,
    underlying_spot_price         REAL DEFAULT 0.0,
    underlying_distance_bps       REAL DEFAULT 0.0,
    underlying_spot_velocity_10s  REAL DEFAULT 0.0,
    underlying_realized_vol_5m    REAL DEFAULT 0.0,
    underlying_realized_vol_1h    REAL DEFAULT 0.0,
    sim_slippage_bps_at_target    REAL DEFAULT 0.0,
    depth_usd_1c                  REAL DEFAULT 0.0,
    depth_usd_5c                  REAL DEFAULT 0.0,
    book_imbalance_momentum_10s   REAL DEFAULT 0.0,
    ask_depth_usd_1c              REAL DEFAULT 0.0,
    bid_depth_usd_1c              REAL DEFAULT 0.0,
    bs_implied_probability        REAL DEFAULT 0.5,
    option_pricing_edge_bps       REAL DEFAULT 0.0,
    execution_attempts            INTEGER DEFAULT 1,
    execution_duration_ms         REAL DEFAULT 0.0,
    fill_percentage               REAL DEFAULT 1.0,
    resolution_latency_seconds    INTEGER DEFAULT 0
);

-- Market regimes: snapshots aggregate market context every 60s
CREATE TABLE IF NOT EXISTS market_regimes (
    regime_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    btc_binance_price REAL DEFAULT 0.0,
    eth_binance_price REAL DEFAULT 0.0,
    cvd_60s_btc REAL DEFAULT 0.0,
    cvd_60s_eth REAL DEFAULT 0.0,
    cvd_5m_btc REAL DEFAULT 0.0,
    cvd_5m_eth REAL DEFAULT 0.0,
    rolling_spread_median REAL DEFAULT 0.0,
    spread_regime_multiplier REAL DEFAULT 1.0,
    implied_vol_proxy REAL DEFAULT 1.0,
    regime_label TEXT DEFAULT 'NORMAL',
    session TEXT DEFAULT 'OFF'
);

-- Scan log: full audit trail of every poll cycle
CREATE TABLE IF NOT EXISTS scan_log (
    scan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at          INTEGER NOT NULL,
    markets_scanned     INTEGER DEFAULT 0,
    markets_flagged     INTEGER DEFAULT 0,
    signals_fired       INTEGER DEFAULT 0,
    orders_placed       INTEGER DEFAULT 0,
    errors              TEXT,    -- nullable
    bot_version         TEXT NOT NULL
);

-- Events log: append-only audit log. NEVER UPDATE or DELETE rows here.
CREATE TABLE IF NOT EXISTS events_log (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time          INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
        -- SCAN/SIGNAL/ORDER_PLACED/FILL/CANCEL/KILL_SWITCH/
        -- WS_DISCONNECT/RATE_LIMIT/ERROR/VERSION_CHANGE
    market_id           TEXT,       -- nullable
    position_id         TEXT,       -- nullable
    payload             TEXT,       -- full JSON string of all relevant fields
    bot_version         TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
CREATE INDEX IF NOT EXISTS idx_markets_flagged ON markets(is_flagged);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(order_status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events_log(event_type);
CREATE INDEX IF NOT EXISTS idx_events_time ON events_log(event_time);
CREATE INDEX IF NOT EXISTS idx_scan_log_time ON scan_log(scanned_at);

-- Wick events: records wicks below 0.50 for duration audits
CREATE TABLE IF NOT EXISTS wick_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         TEXT NOT NULL,
    market_id           TEXT NOT NULL REFERENCES markets(market_id),
    profile_name        TEXT NOT NULL,
    start_time          REAL NOT NULL,
    end_time            REAL NOT NULL,
    duration_ms         REAL NOT NULL,
    resolved_by         TEXT NOT NULL,
    entry_price         REAL NOT NULL,
    trigger_price       REAL NOT NULL,
    exit_price          REAL,
    timestamp           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wick_events_position ON wick_events(position_id);
CREATE INDEX IF NOT EXISTS idx_wick_events_market ON wick_events(market_id);
CREATE INDEX IF NOT EXISTS idx_wick_events_profile ON wick_events(profile_name);

-- Shadow Router Logs: V4.5 Challenger decision telemetry
CREATE TABLE IF NOT EXISTS shadow_router_logs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id             TEXT DEFAULT (lower(hex(randomblob(16)))),
    signal_id               TEXT,
    market_id               TEXT NOT NULL,
    timestamp               DATETIME DEFAULT CURRENT_TIMESTAMP,
    
    -- Versioning
    router_version          TEXT NOT NULL DEFAULT 'v4.5.0-shadow',
    weights_version         TEXT NOT NULL DEFAULT 'v4.5.1',
    feature_schema_version TEXT NOT NULL DEFAULT 'v1.0',
    
    -- Raw Feature Snapshots
    entry_price             REAL,
    cvd_60s                 REAL,
    book_imbalance          REAL,
    streak_depth            INTEGER,
    seconds_to_close        REAL,
    asset                   TEXT,
    
    -- Decision Trace
    legacy_decision         TEXT NOT NULL,
    router_decision         TEXT NOT NULL,
    winning_engine          TEXT NOT NULL,
    winning_score           REAL NOT NULL,
    second_best_score       REAL DEFAULT 0.0,
    score_margin            REAL DEFAULT 0.0,
    reason_rejected         TEXT,
    market_regime           TEXT,
    conviction_level        TEXT,
    raw_scores_json         TEXT,
    
    -- Decoupled Reconciliation (Populated Post-Resolution)
    resolved_at             DATETIME,
    legacy_pnl              REAL,
    realized_router_pnl     REAL,
    delta_pnl               REAL
);

CREATE INDEX IF NOT EXISTS idx_shadow_router_market ON shadow_router_logs(market_id);
CREATE INDEX IF NOT EXISTS idx_shadow_router_decision ON shadow_router_logs(router_decision);
CREATE INDEX IF NOT EXISTS idx_shadow_router_version ON shadow_router_logs(router_version);

-- Shadow Meta-Strategy Logger: observational context for each profile trade
-- Used for offline analysis to determine if dynamic sizing edges exist
CREATE TABLE IF NOT EXISTS meta_strategy_log (
    log_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id             TEXT NOT NULL,          -- FK to profile_positions.position_id
    profile_name            TEXT NOT NULL,
    market_id               TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    window_minutes          INTEGER NOT NULL,
    opened_at               INTEGER NOT NULL,       -- epoch timestamp
    
    -- Sequential State: consecutive wins/losses for this (profile, symbol, window) BEFORE this trade
    seq_consecutive_wins    INTEGER DEFAULT 0,      -- 0 = last trade was a loss or no history
    seq_consecutive_losses  INTEGER DEFAULT 0,      -- 0 = last trade was a win or no history
    seq_last_5_outcomes     TEXT DEFAULT '',         -- e.g. "WLWWL" (most recent first)
    
    -- Trailing Win Rate: rolling performance for this profile
    trailing_7d_total       INTEGER DEFAULT 0,      -- total resolved trades in last 7 days
    trailing_7d_wins        INTEGER DEFAULT 0,
    trailing_7d_wr          REAL DEFAULT 0.0,       -- wins / total
    trailing_14d_total      INTEGER DEFAULT 0,
    trailing_14d_wins       INTEGER DEFAULT 0,
    trailing_14d_wr         REAL DEFAULT 0.0,
    
    -- Breakeven Context
    avg_entry_price_7d      REAL DEFAULT 0.0,       -- average entry price over last 7 days
    breakeven_wr_7d         REAL DEFAULT 0.5,       -- entry_price / (entry_price + (1-entry_price)*0.98)
    edge_over_breakeven_7d  REAL DEFAULT 0.0,       -- trailing_7d_wr - breakeven_wr_7d
    
    -- Cross-Profile Regime: how are OTHER profiles performing?
    -- For cheap_reversal trades, log stop_loss's trailing WR (and vice versa)
    cross_profile_name      TEXT DEFAULT '',         -- the "opposing" profile name
    cross_profile_7d_wr     REAL DEFAULT 0.0,       -- opposing profile's 7-day WR
    cross_profile_7d_total  INTEGER DEFAULT 0,
    
    -- Regime Classification (derived, not used for decisions)
    regime_signal           TEXT DEFAULT 'UNKNOWN',  -- REVERSAL_FAVORED / TREND_FAVORED / NEUTRAL / UNKNOWN
    
    -- Day/Hour context (duplicated from profile_positions for easy querying)
    hour_et                 INTEGER DEFAULT 0,
    day_of_week             TEXT DEFAULT '',
    is_weekend              INTEGER DEFAULT 0,
    
    -- Outcome (filled when position resolves)
    closed_at               INTEGER,
    realised_pnl            REAL,
    trade_won               INTEGER                 -- 1 = win, 0 = loss
);

CREATE INDEX IF NOT EXISTS idx_meta_log_profile ON meta_strategy_log(profile_name);
CREATE INDEX IF NOT EXISTS idx_meta_log_position ON meta_strategy_log(position_id);
CREATE INDEX IF NOT EXISTS idx_meta_log_opened ON meta_strategy_log(opened_at);



