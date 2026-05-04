# Graph Report - .  (2026-04-22)

## Corpus Check
- 0 files · ~0 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 652 nodes · 1047 edges · 87 communities detected
- Extraction: 69% EXTRACTED · 31% INFERRED · 0% AMBIGUOUS · INFERRED: 327 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]

## God Nodes (most connected - your core abstractions)
1. `main()` - 32 edges
2. `ClobClient` - 32 edges
3. `upsert_market()` - 27 edges
4. `L2Auth` - 19 edges
5. `TelegramClient` - 17 edges
6. `PositionManager` - 17 edges
7. `BotConfig` - 16 edges
8. `GammaClient` - 16 edges
9. `KillSwitch` - 16 edges
10. `load_config()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `start_dashboard_server()`  [INFERRED]
  /Users/rahulkumar/Documents/reversebot/src/main.py → src/api/dashboard.py
- `serve_dashboard()` --calls--> `load_config()`  [INFERRED]
  src/api/dashboard.py → /Users/rahulkumar/Documents/reversebot/config/config.py
- `api_set_config()` --calls--> `load_runtime_overrides()`  [INFERRED]
  src/api/dashboard.py → /Users/rahulkumar/Documents/reversebot/config/config.py
- `test_lock_is_per_market()` --calls--> `get_market_lock()`  [INFERRED]
  /Users/rahulkumar/Documents/reversebot/tests/test_position_mgr.py → src/strategy/position_mgr.py
- `test_same_market_same_lock()` --calls--> `get_market_lock()`  [INFERRED]
  /Users/rahulkumar/Documents/reversebot/tests/test_position_mgr.py → src/strategy/position_mgr.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (46): ClobClient, CLOB API Client — Order Execution for Polymarket.  Handles: - FOK order placemen, Fetch the current orderbook for a token.         Used for slippage guard — check, Place an order on the CLOB.          ENFORCED: Only FOK orders are accepted., Simulate an order fill for paper trading mode., Poll order status to confirm fill.          MISTAKE 7 GUARD: HTTP 200 from place, Poll order status until confirmed MATCHED/CANCELLED or timeout.         Returns, Cancel a single order. Returns True if successful. (+38 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (57): close_position(), count_open_positions(), create_position(), get_flagged_markets(), get_market(), get_position(), get_total_exposure(), has_open_position_for_market() (+49 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (22): DecisionEngine, get_core_context(), Extract the core event context by stripping dates, times, and window markers., Orchestrates the full trading decision pipeline:     Signal → Validate → EV → Sl, get_logger(), Structured logging configuration for Polymarket Reverse Bot. Uses structlog with, A StreamHandler that ignores BrokenPipeError.     Useful when running in backgro, Configure structlog with JSON rendering, rotating file handler,     and stdout o (+14 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (22): main(), GammaClient, _normalize_tag_slug(), _parse_timestamp(), Gamma API Client — Market Discovery & Polling for Polymarket.  Polls the Gamma A, Make a rate-limited GET request to the Gamma API., Get real world UTC time from API headers., ULTIMATE FIX: Predictive Slug Hashing.         Polymarket's search index is brok (+14 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (18): L2Auth, L2 Authentication — HMAC-SHA256 Headers for Polymarket CLOB API.  Every CLOB req, Get current timestamp adjusted for server time offset., Create HMAC-SHA256 signature for a CLOB API request.         Matches official Po, Log auth failure details for debugging.         NEVER logs the API secret or pas, L2 HMAC-SHA256 authentication for Polymarket CLOB API.     Handles header constr, Reset after a successful authenticated request., Record a 401 failure. Returns True if kill threshold reached. (+10 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (18): MarketScanner, _normalize_symbol(), Market Scanner — Poll & Threshold Detection for Polymarket Reverse Bot., Stop the background discovery task., run_all(), verify_issue_2_no_price_logic(), verify_issue_3_share_rounding(), WebSocket Client for Polymarket CLOB real-time feeds.  Subscribes to: book, pric (+10 more)

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (29): BaseModel, api_clear_history(), api_delete_history(), api_get_config(), api_history(), api_pnl_series(), api_positions(), api_set_config() (+21 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (20): KillSwitch, Kill Switch — Emergency Halt & Cancel-All for Polymarket Reverse Bot.  NON-NEGOT, Reset consecutive error count after a successful API call., Fire the kill switch. Follows the exact sequence specified., Internal execution of the kill sequence steps., Emergency halt system for the Reverse Bot.     cancel_all_orders() is ALWAYS cal, Register a callback to run after kill switch fires., Record an API error. Returns True if kill threshold reached. (+12 more)

### Community 8 - "Community 8"
Cohesion: 0.16
Nodes (25): Calculate total available volume at current ask price for a side.         Used f, Insert or update a market record., upsert_market(), _mock_config(), Tests for Signal Validator — all 9 checks individually testable. Tests for scann, Create a mock BotConfig., Create a sample valid market for validation., _sample_market() (+17 more)

### Community 9 - "Community 9"
Cohesion: 0.11
Nodes (18): analyze(), analyze(), analyze(), GammaClient, export(), close_db(), init_db(), Initialize database: create file, apply schema, run migrations.     Returns an o (+10 more)

### Community 10 - "Community 10"
Cohesion: 0.11
Nodes (10): get_performance_summary(), Calculate high-level performance metrics from the positions table., Telegram Client — Remote Control & Reporting for Polymarket Reverse Bot. Uses ai, Start or restart the auto-refresh loop for positions., Stop the auto-refresh loop., Edit the positions message every 60 seconds., Escape HTML-sensitive characters in dynamic text., Manages the Telegram bot interface with a dynamic dashboard. (+2 more)

### Community 11 - "Community 11"
Cohesion: 0.13
Nodes (19): _env_bool(), _env_float(), _env_int(), _env_int_list(), _env_list(), load_config(), load_runtime_overrides(), Configuration loader for Polymarket Reverse Bot. All tunable parameters loaded f (+11 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (11): available_tokens(), RateLimiter, Token Bucket Rate Limiter for Polymarket API.  Two separate buckets: - Orders: 5, Dual-bucket rate limiter for Polymarket API.     Separate buckets for order oper, Args:             orders_per_min: Max orders per minute (default 55, hard limit, Acquire a token for placing an order.         Consumes from BOTH order and API b, Async-safe token bucket rate limiter.     Refills tokens at a constant rate. Whe, Args:             name: Bucket name for logging (e.g., "orders", "api_calls") (+3 more)

### Community 13 - "Community 13"
Cohesion: 0.15
Nodes (10): Risk Limits — Daily Loss, Exposure Cap, Position Count for Reverse Bot.  Checked, Check if kill switch should fire based on risk limits.         This is called fr, Enforces position limits, exposure caps, and daily loss limits.     All checks r, Run all risk checks. Returns (allowed, reason).         If any check fails, retu, Check if daily loss limit has been reached., Check if max position count has been reached., Check if adding a trade would exceed max exposure., RiskLimits (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.16
Nodes (9): L1Auth, L1 Authentication — EIP-712 Wallet Signing for Polymarket.  Uses eth_account to, Sign EIP-712 typed data.         Returns the hex signature., Create the EIP-712 signature needed to derive L2 API credentials.          Args:, Sign an order with EIP-712 for CLOB submission.          Args:             order, L1 EIP-712 authentication handler.     Signs typed data to derive L2 API credent, Initialize with private key.          Args:             private_key: Hex private, Sign a plaintext message with the private key.         Returns the hex signature (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.23
Nodes (5): HFTMarketManager, O(1) style discovery using cached markets.         Finds current active market a, Continuously monitor and rotate markets., Fetch markets matching the criteria. Cache results to avoid spamming API (O(1) l, Check if market meets strict trading requirements.

### Community 16 - "Community 16"
Cohesion: 0.2
Nodes (7): api_control(), HealthMonitor, WebSocket Health Monitor — Heartbeat Check for Polymarket Reverse Bot.  Monitors, Monitors WebSocket connection health.     Fires kill switch on prolonged silence, Start the health monitor as a background task., Stop the health monitor., Background loop checking WS health every 30 seconds.

### Community 17 - "Community 17"
Cohesion: 0.25
Nodes (4): Initialize the rejections database and schema., Log a rejected signal with full verbose context., Handles verbose logging of rejected signals into a dedicated SQLite database., RejectionLogger

### Community 18 - "Community 18"
Cohesion: 0.29
Nodes (4): shares = size_usdc / no_price — NOT size_usdc., The wrong formula (shares = size_usdc) should fail the invariant., MISTAKE 6: Fee-adjusted EV at different price points.         2% fee on net winn, TestSharesCalculation

### Community 19 - "Community 19"
Cohesion: 0.6
Nodes (2): MockGammaClient, run_test()

### Community 20 - "Community 20"
Cohesion: 0.4
Nodes (0): 

### Community 21 - "Community 21"
Cohesion: 0.67
Nodes (3): fetch_and_filter_markets(), is_crypto_short_window(), Checks if a market question implies a 5-minute or 15-minute crypto window.

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (2): fetch_and_filter_markets(), is_crypto_short_window()

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (2): fetch_live(), is_match()

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (0): 

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (0): 

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (0): 

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (0): 

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (0): 

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (0): 

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (0): 

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (0): 

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (0): 

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (0): 

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (0): 

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (0): 

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (0): 

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (0): 

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (0): 

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (0): 

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (0): 

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (0): 

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (0): 

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (0): 

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (0): 

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (0): 

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Opening a position should go PENDING → OPEN.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): If order placement fails, position should be FAILED.

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Two concurrent open_position calls for the same market should be serialized.

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Different markets should use separate locks.

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): Same market should return the same lock.

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): Attempting a GTC order should raise AssertionError.

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): FOK order should work without assertion error.

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): The single most important test: verify cancel_all_orders() is         called BEF

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): Kill flag must be set before any async operations.

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): Triggering kill switch twice should not run sequence twice.

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Kill switch must work even without a WebSocket client.

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): Kill switch event should be written to events_log.

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): All 5 required tables must exist.

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): Partial unique index on positions(market_id) WHERE status='OPEN' must exist.

### Community 60 - "Community 60"
Cohesion: 1.0
Nodes (1): WAL journal mode should be active for concurrent reads.

### Community 61 - "Community 61"
Cohesion: 1.0
Nodes (1): Insert a market, then retrieve it.

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): Upserting the same market_id updates fields.

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): Create a position and verify default status is PENDING.

### Community 64 - "Community 64"
Cohesion: 1.0
Nodes (1): Two OPEN positions for the same market_id must be rejected by the DB.

### Community 65 - "Community 65"
Cohesion: 1.0
Nodes (1): MISTAKE 3 guard: shares * entry_price ≈ size_usdc.         This asserts the corr

### Community 66 - "Community 66"
Cohesion: 1.0
Nodes (1): Events log should accumulate, never lose entries.

### Community 67 - "Community 67"
Cohesion: 1.0
Nodes (1): Paper positions are tracked separately from live positions.

### Community 68 - "Community 68"
Cohesion: 1.0
Nodes (0): 

### Community 69 - "Community 69"
Cohesion: 1.0
Nodes (1): Volume >= min should pass.

### Community 70 - "Community 70"
Cohesion: 1.0
Nodes (1): Volume < min should fail.

### Community 71 - "Community 71"
Cohesion: 1.0
Nodes (1): If a market already has an OPEN position, check 4 should fail.

### Community 72 - "Community 72"
Cohesion: 1.0
Nodes (1): When at max positions, check 7 should fail.

### Community 73 - "Community 73"
Cohesion: 1.0
Nodes (1): Only 1 cycle above threshold should fail check 8.

### Community 74 - "Community 74"
Cohesion: 1.0
Nodes (1): Non-whitelisted category should fail check 9.

### Community 75 - "Community 75"
Cohesion: 1.0
Nodes (1): A perfectly valid signal should pass all 9 checks.

### Community 76 - "Community 76"
Cohesion: 1.0
Nodes (0): 

### Community 77 - "Community 77"
Cohesion: 1.0
Nodes (0): 

### Community 78 - "Community 78"
Cohesion: 1.0
Nodes (1): Number of consecutive 401 auth failures.

### Community 79 - "Community 79"
Cohesion: 1.0
Nodes (1): True if 401 threshold reached — kill switch should fire.

### Community 80 - "Community 80"
Cohesion: 1.0
Nodes (1): Public wallet address.

### Community 81 - "Community 81"
Cohesion: 1.0
Nodes (0): 

### Community 82 - "Community 82"
Cohesion: 1.0
Nodes (0): 

### Community 83 - "Community 83"
Cohesion: 1.0
Nodes (1): Current available tokens (approximate, without lock).

### Community 84 - "Community 84"
Cohesion: 1.0
Nodes (0): 

### Community 85 - "Community 85"
Cohesion: 1.0
Nodes (0): 

### Community 86 - "Community 86"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **215 isolated node(s):** `Market Scanner — Poll & Threshold Detection for Polymarket Reverse Bot.`, `Stop the background discovery task.`, `Configuration loader for Polymarket Reverse Bot. All tunable parameters loaded f`, `Get required env var or exit with descriptive error.`, `Get float env var with default.` (+210 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 24`** (2 nodes): `check()`, `check_markets.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (2 nodes): `check()`, `check_events.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (2 nodes): `main()`, `parse_debug.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (2 nodes): `fetch_markets()`, `scratch.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (2 nodes): `check()`, `debug_prices.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (2 nodes): `test_filter()`, `test_gamma_filter.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (2 nodes): `check()`, `scan_all.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (2 nodes): `check()`, `check_prices.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (2 nodes): `check()`, `find_15m.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (2 nodes): `check()`, `deep_search_15m.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (2 nodes): `test_schemas()`, `test_payloads.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (2 nodes): `main()`, `fetch_test.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (2 nodes): `main()`, `debug_markets.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `fetch_trades_v2.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `test_db.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `check_statuses.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `fetch_trades.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `check_db2.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `check_counts.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `check_db.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `clear_fake_wins.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Opening a position should go PENDING → OPEN.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `If order placement fails, position should be FAILED.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Two concurrent open_position calls for the same market should be serialized.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Different markets should use separate locks.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `Same market should return the same lock.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `Attempting a GTC order should raise AssertionError.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): `FOK order should work without assertion error.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `The single most important test: verify cancel_all_orders() is         called BEF`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `Kill flag must be set before any async operations.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `Triggering kill switch twice should not run sequence twice.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Kill switch must work even without a WebSocket client.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `Kill switch event should be written to events_log.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `All 5 required tables must exist.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `Partial unique index on positions(market_id) WHERE status='OPEN' must exist.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (1 nodes): `WAL journal mode should be active for concurrent reads.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (1 nodes): `Insert a market, then retrieve it.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): `Upserting the same market_id updates fields.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): `Create a position and verify default status is PENDING.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 64`** (1 nodes): `Two OPEN positions for the same market_id must be rejected by the DB.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 65`** (1 nodes): `MISTAKE 3 guard: shares * entry_price ≈ size_usdc.         This asserts the corr`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 66`** (1 nodes): `Events log should accumulate, never lose entries.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 67`** (1 nodes): `Paper positions are tracked separately from live positions.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 68`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 69`** (1 nodes): `Volume >= min should pass.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 70`** (1 nodes): `Volume < min should fail.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 71`** (1 nodes): `If a market already has an OPEN position, check 4 should fail.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 72`** (1 nodes): `When at max positions, check 7 should fail.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 73`** (1 nodes): `Only 1 cycle above threshold should fail check 8.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 74`** (1 nodes): `Non-whitelisted category should fail check 9.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 75`** (1 nodes): `A perfectly valid signal should pass all 9 checks.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 76`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 77`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 78`** (1 nodes): `Number of consecutive 401 auth failures.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 79`** (1 nodes): `True if 401 threshold reached — kill switch should fire.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 80`** (1 nodes): `Public wallet address.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 81`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 82`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 83`** (1 nodes): `Current available tokens (approximate, without lock).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 84`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 85`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 86`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 2` to `Community 0`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 12`, `Community 13`, `Community 14`, `Community 16`, `Community 17`?**
  _High betweenness centrality (0.368) - this node is a cross-community bridge._
- **Why does `ClobClient` connect `Community 0` to `Community 8`, `Community 9`, `Community 2`, `Community 4`?**
  _High betweenness centrality (0.081) - this node is a cross-community bridge._
- **Why does `GammaClient` connect `Community 3` to `Community 2`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Are the 27 inferred relationships involving `main()` (e.g. with `load_config()` and `setup_logging()`) actually correct?**
  _`main()` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `ClobClient` (e.g. with `PositionManager` and `Position Manager — Open/Monitor/Close Positions for Polymarket Reverse Bot.  Han`) actually correct?**
  _`ClobClient` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `upsert_market()` (e.g. with `.run_scan_cycle()` and `test_successful_open()`) actually correct?**
  _`upsert_market()` has 25 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `L2Auth` (e.g. with `.test_headers_contain_required_fields()` and `.test_nonce_is_unique_per_request()`) actually correct?**
  _`L2Auth` has 9 INFERRED edges - model-reasoned connections that need verification._