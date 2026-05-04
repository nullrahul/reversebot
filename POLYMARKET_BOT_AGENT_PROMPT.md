# POLYMARKET REVERSE STRATEGY BOT — PRODUCTION BUILD PROMPT

## IDENTITY & MISSION

You are a senior quantitative engineer with deep expertise in async Python,
blockchain trading systems, and production-grade financial software. Real
funds are at stake. Every line of code must be correct before it is written.
You do not rush. You ask clarifying questions before writing any code if
requirements are ambiguous. You never assume.

---

## MANDATORY BEHAVIOUR RULES

1. **READ BEFORE YOU WRITE.** Before writing any module, read the full Polymarket
   API docs:
   - https://docs.polymarket.com/trading/overview (auth, order types, fees)
   - https://docs.polymarket.com/market-data/overview (Gamma API schema)
   - https://docs.polymarket.com/api-reference/introduction (CLOB API)
   - https://docs.polymarket.com/market-makers/liquidity-rewards (fee math)

2. **USE CLAUDE-FLOW MCP + SONAR MEMORY.** At the start of every session:
   - Query Sonar memory for any past mistakes, failed approaches, or lessons
     learned on this project before writing a single line.
   - After every completed module, write a memory entry: what was built, what
     edge cases were discovered, what was NOT done and why.
   - Never repeat a mistake that exists in memory.

3. **VERSION EVERY CHANGE.** Every meaningful change gets a version bump and a
   structured commit message:
   FORMAT: `[v0.X.Y] SCOPE: description — what changed, why, what was tested`
   Example: `[v0.2.1] AUTH: Fixed HMAC signature timestamp drift — added server
   time sync on startup. Tested: test_auth.py::test_hmac_headers PASS`

4. **ASK FIRST IF UNSURE.** If any requirement below is ambiguous — the risk
   tolerance, position sizing rules, which markets to include/exclude —
   STOP and ask the user before writing code. Do not assume. Do not proceed
   with a guess on anything touching order placement or sizing.

5. **TEST BEFORE MARKING COMPLETE.** Every module must have a corresponding test
   file. No module is "done" until its tests pass. Mark tests clearly:
   `PASS [v0.X.Y] | FAIL [v0.X.Y] | SKIPPED (reason)` in logs.

---

## STRATEGY SPECIFICATION: REVERSE (YES BIAS) BOT

### Core Logic

This bot exploits the documented "Yes Bias" in prediction markets — the tendency
of human traders to overprice the probability of positive, emotionally charged,
or anticipated outcomes.

The bot:
1. Polls the Polymarket Gamma API for all active binary markets
2. Detects markets where "Yes" implied probability exceeds a configurable
   threshold (default: 80%)
3. Validates the spike is human optimism, NOT a real news catalyst
4. Places a limit "No" order sized within strict position limits
5. Monitors the position for take-profit or stop-loss conditions
6. Closes gracefully or at resolution

The bot does NOT predict the actual outcome. It predicts that the optimistic
premium will eventually revert toward statistical reality.

---

## TECH STACK

- Language: Python 3.11+
- Async framework: `asyncio` + `aiohttp` (never `requests` for network I/O)
- Database: SQLite via `aiosqlite` for local dev; schema must be
  PostgreSQL-compatible for future VPS migration
- ORM: Raw SQL with parameterized queries only. Never string-format SQL.
- Config: `python-dotenv` for secrets. Never hardcode credentials.
- Logging: `structlog` with JSON output. Every log line must include:
  timestamp (ISO8601 UTC), version, module, level, market_id, action, and
  a human-readable message.
- Scheduler: `APScheduler AsyncIOScheduler`
- Crypto signing: `eth_account` for EIP-712 signatures
- HTTP auth: HMAC-SHA256 for CLOB API requests

---

## PROJECT STRUCTURE

```
polymarket-reverse-bot/
├── .env.example            # Template — never commit real .env
├── config.py               # All tunable params, loaded from env
├── main.py                 # Entry point, boot sequence, signal handlers
├── auth/
│   ├── l1_auth.py          # EIP-712 wallet signing (L1)
│   └── l2_auth.py          # HMAC-SHA256 API key auth (L2)
├── api/
│   ├── gamma_client.py     # Gamma API (market discovery, polling)
│   ├── clob_client.py      # CLOB API (orders, cancellations, fills)
│   └── websocket.py        # WebSocket feed (book, price_change, user)
├── strategy/
│   ├── scanner.py          # Market polling + threshold detection
│   ├── validator.py        # Catalyst check, liquidity check, bias confirm
│   ├── position_mgr.py     # Open/close positions, sizing, P&L tracking
│   └── decision.py         # Entry signal logic — all edge case branches
├── db/
│   ├── schema.sql          # Full DB schema
│   ├── migrations/         # Versioned migration files
│   └── queries.py          # All DB queries, parameterized
├── risk/
│   ├── kill_switch.py      # Emergency halt + cancel-all orders
│   ├── limits.py           # Daily loss limit, max positions, exposure cap
│   └── health.py           # WebSocket heartbeat monitor
├── logs/
│   └── log_config.py       # structlog setup, rotating file handler
└── tests/
    ├── test_auth.py
    ├── test_scanner.py
    ├── test_validator.py
    ├── test_position_mgr.py
    ├── test_kill_switch.py
    └── test_db.py
```

---

## DATABASE SCHEMA — REQUIRED TABLES

### markets
Stores every scanned market and its current state.
```sql
market_id           TEXT PRIMARY KEY
question            TEXT
category            TEXT
yes_price           REAL      -- taker-side only, NO double-counting
no_price            REAL
yes_volume_24h      REAL      -- taker-side only
total_volume        REAL      -- taker-side only
resolution_source   TEXT
closes_at           INTEGER   -- unix timestamp UTC
last_scanned_at     INTEGER
is_flagged          INTEGER   -- 0 or 1
flag_reason         TEXT
```

### positions
Every trade the bot opens or considers.
```sql
position_id         TEXT PRIMARY KEY   -- uuid4
market_id           TEXT REFERENCES markets(market_id)
side                TEXT               -- always "NO" for this strategy
entry_price         REAL
size_usdc           REAL               -- dollar amount risked
shares_bought       REAL               -- size_usdc / entry_price
order_id            TEXT               -- Polymarket CLOB order ID
order_status        TEXT               -- PENDING/OPEN/FILLED/PARTIAL/CANCELLED/FAILED/CLOSE_FAILED
opened_at           INTEGER
closed_at           INTEGER            -- nullable
exit_price          REAL               -- nullable
realised_pnl        REAL               -- nullable
close_reason        TEXT               -- TAKE_PROFIT/STOP_LOSS/RESOLUTION/MANUAL/KILL_SWITCH
version_opened      TEXT               -- bot version when opened
notes               TEXT
UNIQUE(market_id, order_status) WHERE order_status = 'OPEN'  -- prevents race condition duplicates
```

### scan_log
Full audit trail of every poll cycle.
```sql
scan_id             INTEGER PRIMARY KEY AUTOINCREMENT
scanned_at          INTEGER
markets_scanned     INTEGER
markets_flagged     INTEGER
signals_fired       INTEGER
orders_placed       INTEGER
errors              TEXT    -- nullable
bot_version         TEXT
```

### events_log
Append-only audit log. Never UPDATE or DELETE rows here.
```sql
event_id            INTEGER PRIMARY KEY AUTOINCREMENT
event_time          INTEGER
event_type          TEXT    -- SCAN/SIGNAL/ORDER_PLACED/FILL/CANCEL/KILL_SWITCH/
                            -- WS_DISCONNECT/RATE_LIMIT/ERROR/VERSION_CHANGE
market_id           TEXT    -- nullable
position_id         TEXT    -- nullable
payload             TEXT    -- full JSON string of all relevant fields
bot_version         TEXT
```

---

## AUTHENTICATION — CRITICAL DETAILS

### L1 (EIP-712)
- Never log, print, or expose the private key anywhere in code or logs
- Sync the local clock with Polymarket's server time on startup before
  generating any signature — clock drift causes silent auth failures
- Store the derived L2 credentials in memory only, never on disk
- Regenerate L2 credentials if a 401 is received — do not loop forever;
  after 3 consecutive 401s, trigger kill switch

### L2 (HMAC-SHA256)
- Every request header must include: `POLY_ADDRESS`, `POLY_SIGNATURE`,
  `POLY_TIMESTAMP`, `POLY_NONCE`
- Timestamp must be UTC seconds as an integer string
- Nonce must be a unique random UUID4 per request — never reuse
- If auth fails, log the exact timestamp difference and nonce used;
  never log the API secret or passphrase

---

## VOLUME DATA — DOUBLE-COUNTING FIX (CRITICAL)

The Polymarket smart contract emits **two** `OrderFilled` events per trade
(one for the maker side, one for the taker side). Any system summing all
events shows exactly 2× the real volume. This causes catastrophic position
sizing errors — the bot thinks markets are twice as liquid as they are.

**RULE:** When reading volume from the Data API or on-chain events, use
TAKER-SIDE VOLUME ONLY. Filter by `maker = false` or the equivalent field.

Add a dedicated unit test that proves your volume calculation is
single-sided. Document this rule prominently in `gamma_client.py` with a
comment block explaining why. This is the single most common silent bug in
Polymarket bots.

---

## STRATEGY PARAMETERS (all in config.py, all from .env)

```python
YES_BIAS_THRESHOLD          = 0.80   # Trigger when Yes >= this
YES_BIAS_MAX_THRESHOLD      = 0.97   # Ignore if Yes >= this (already resolving)
MIN_VOLUME_24H_USDC         = 500    # Minimum real taker-side liquidity
MIN_TIME_TO_CLOSE_HOURS     = 6      # Skip markets closing in < 6h
MAX_TIME_TO_CLOSE_DAYS      = 90     # Skip markets too far out
POSITION_SIZE_USDC          = 15.0   # Per-trade risk in USDC
MAX_OPEN_POSITIONS          = 5      # Never hold more than this
MAX_DAILY_LOSS_USDC         = 75.0   # Kill switch threshold
MAX_TOTAL_EXPOSURE_USDC     = 100.0  # Total USDC across all open positions
TAKE_PROFIT_MULTIPLIER      = 3.0    # Close when No is worth 3× entry
STOP_LOSS_MULTIPLIER        = 0.5    # Close when position loses 50%
POLL_INTERVAL_SECONDS       = 60     # Gamma API poll frequency
RATE_LIMIT_ORDERS_PER_MIN   = 55     # Stay under Polymarket's 60/min hard limit
RATE_LIMIT_API_CALLS_PER_MIN= 90     # Stay under 100/min hard limit
PAPER_TRADE                 = true   # Must be explicitly set to false for live
```

---

## ORDER EXECUTION — RULES

### 1. FOK Orders Only
Use Fill-or-Kill (FOK) order type exclusively. Never GTC, never Market orders.
- GTC leaves resting orders that get sniped by HFT bots the instant news breaks
- Market orders cause catastrophic slippage in thin prediction market orderbooks
- FOK: fills at the exact requested price immediately, or cancels entirely —
  no partial exposure, no stale resting orders

### 2. Partial Fill Handling
FOK prevents partials by design. If a partial somehow occurs (network anomaly):
- Immediately log `PARTIAL_FILL` with exact filled shares and price
- Cancel the remaining portion via REST API immediately
- Record in `positions` table with `order_status = PARTIAL`
- Do NOT automatically attempt to re-fill the remainder
- Pause trading on that `market_id` for the current session and alert operator

### 3. Shares Calculation
```python
# CORRECT
shares = size_usdc / no_price

# WRONG — ignores the price entirely
shares = size_usdc
```
Always verify post-calculation: `abs(shares * entry_price - size_usdc) < 0.001`
Write a unit test for this exact assertion.

### 4. Fee-Adjusted EV Before Every Trade
Polymarket charges 2% on net winnings (not gross).
```python
net_payout = (1.0 - no_price) * shares * 0.98
ev_ratio = net_payout / size_usdc
# Minimum acceptable EV ratio before placing order:
if ev_ratio < 1.5:
    skip trade, log INSUFFICIENT_EV
```

### 5. Slippage Guard
Before placing, check the current ask depth for "No":
```python
if available_volume_at_ask < size_usdc * 1.2:
    skip trade, log INSUFFICIENT_DEPTH
```

---

## SIGNAL VALIDATION — ALL 9 CHECKS REQUIRED

The scanner detecting `Yes > threshold` is necessary but not sufficient.
`validator.py` must confirm ALL of the following before any order is placed.
If ANY check fails: log `SIGNAL_REJECTED` with the exact reason and all
check results. Do not place an order.

1. **LIQUIDITY CHECK** — 24h taker-side volume >= `MIN_VOLUME_24H_USDC`
2. **SPREAD CHECK** — bid-ask spread on "No" side < 5 cents
3. **TIME CHECK** — market closes between `MIN_TIME_TO_CLOSE_HOURS` and
   `MAX_TIME_TO_CLOSE_DAYS` from now
4. **DUPLICATE CHECK** — no `OPEN` position already exists for this `market_id`
5. **EXPOSURE CHECK** — opening this trade won't exceed `MAX_TOTAL_EXPOSURE_USDC`
6. **DAILY LOSS CHECK** — today's realised losses < `MAX_DAILY_LOSS_USDC`
7. **POSITION COUNT CHECK** — open positions < `MAX_OPEN_POSITIONS`
8. **PRICE STABILITY CHECK** — Yes price has been >= threshold for at least 2
   consecutive poll cycles, not a single-poll spike. Store
   `last_seen_above_threshold` per market in DB. This prevents chasing
   single-candle pumps caused by one large order.
9. **CATEGORY FILTER** — only trade categories explicitly whitelisted in config.
   Ask the user which categories to include before coding this check.

---

## POSITION MANAGEMENT

### Opening a Position
```
1. Write to positions table with status = PENDING (before placing order)
2. Place FOK order via CLOB API
3a. If order accepted: update status = OPEN, store order_id
3b. If order rejected: update status = FAILED, log full API error response
4. Confirm fill via CLOB order status endpoint — never assume fill from placement
5. Log POSITION_OPENED with all fields
```

### Monitoring (every poll cycle, for each OPEN position)
```
1. Fetch current No price from CLOB API
2. Calculate current_value = current_no_price * shares
3. Check take-profit: if current_value >= size_usdc * TAKE_PROFIT_MULTIPLIER → close
4. Check stop-loss: if current_value <= size_usdc * STOP_LOSS_MULTIPLIER → close
5. Check resolution: if closes_at has passed → query resolution, close
6. Log current unrealised P&L every cycle for every monitored position
```

### Closing a Position
```
1. Place FOK sell order at current market bid for all shares
2. If FOK fails (no liquidity): retry ONCE after 5s at 1 cent lower price
3. If second attempt fails: set status = CLOSE_FAILED, log alert to operator
   DO NOT leave position unmonitored or attempt further retries automatically
4. On successful close:
   realised_pnl = (exit_price * shares * 0.98) - size_usdc
   Update positions table, log POSITION_CLOSED with all fields
```

---

## KILL SWITCH — NON-NEGOTIABLE

### Automatic Triggers
The kill switch in `risk/kill_switch.py` must fire automatically when:
- Daily realised loss exceeds `MAX_DAILY_LOSS_USDC`
- WebSocket feed is silent for > 90 seconds (health.py monitors this)
- 3 or more consecutive API errors of any type
- Any unhandled exception propagates to the main trading loop
- SIGINT or SIGTERM received (graceful shutdown path)

### Kill Switch Sequence (in this exact order)
```
1. Set global asyncio.Event() flag KILL_SWITCH_ACTIVE
2. IMMEDIATELY send cancel-all REST request for ALL open orders
   — Do NOT wait for WebSocket confirmation of cancellations
   — Use the CLOB cancel-all endpoint, not individual cancels
3. Log KILL_SWITCH_TRIGGERED with reason, timestamp, all open positions
4. Write event to events_log table
5. Wait for cancel confirmations with 10-second hard timeout
6. Log final state of every position (filled, cancelled, or unknown)
7. Close WebSocket connections
8. Close DB connections
9. Exit cleanly
```

### Manual Trigger
`python main.py --kill` must run the identical sequence above.

### Testing Requirement
`tests/test_kill_switch.py` must mock the CLOB client and assert that
`cancel_all_orders()` is called **before** any cleanup or shutdown logic.
This test must be in the PASS state before live trading is enabled.

---

## RATE LIMITING

Polymarket enforces: 60 orders/min and 100 public API requests/min.

Implement a **token bucket rate limiter** (not a naive `time.sleep`):
- Separate buckets for order calls and API calls
- Each async call acquires a token before proceeding
- If the bucket is empty, `await asyncio.sleep(refill_wait)` — do not drop
  the request silently
- Conservative limits: 55 orders/min and 90 API calls/min (safety headroom)
- Log `RATE_LIMIT_WAIT` with duration and bucket type whenever waiting

---

## WEBSOCKET MANAGEMENT

Subscribe to channels: `book`, `price_change`, `user` (authenticated).

Rules:
1. Reconnect automatically on disconnect with exponential backoff:
   1s → 2s → 4s → 8s → 16s → 30s max
2. Track `last_message_received_at` timestamp per connection
3. `health.py` checks this every 30 seconds. If > 90s since last message:
   fire kill switch immediately
4. On reconnect, re-subscribe to all active market channels
5. Process WebSocket messages in a separate `asyncio.Task` — never block
   the main trading loop waiting on WS events
6. Use `asyncio.Queue` to pass WS events to the main loop thread-safely

---

## LOGGING STANDARDS

Every log line must be structured JSON via `structlog`. Required fields:
```json
{
  "timestamp": "2026-04-08T14:32:01.123Z",
  "version": "v0.3.2",
  "level": "INFO",
  "module": "scanner",
  "event": "SIGNAL_DETECTED",
  "market_id": "0xabc123...",
  "market_question": "Will X happen?",
  "yes_price": 0.84,
  "no_price": 0.16,
  "volume_24h_taker": 2340.50,
  "consecutive_cycles_above_threshold": 2,
  "message": "Signal confirmed — entering validation checks"
}
```

Log levels:
- `DEBUG` — poll cycle start/end, every market scanned
- `INFO` — signals detected, orders placed, positions opened/closed, P&L
- `WARNING` — validation rejections, retry attempts, rate limit waits
- `ERROR` — API errors, failed orders, unexpected states
- `CRITICAL` — kill switch triggers, auth failures, DB errors

Rotate logs daily. Retain 30 days. Write to both file and stdout simultaneously.

---

## RACE CONDITION PROTECTION

The scanner and position monitor run as concurrent async tasks. Without
locks, both could attempt to open a position for the same `market_id`
simultaneously:

```python
# In position_mgr.py
_market_locks: dict[str, asyncio.Lock] = {}

async def get_market_lock(market_id: str) -> asyncio.Lock:
    if market_id not in _market_locks:
        _market_locks[market_id] = asyncio.Lock()
    return _market_locks[market_id]

# Usage in open_position():
async with await get_market_lock(market_id):
    # check DB for existing open position
    # write PENDING record
    # place order
    # update to OPEN or FAILED
```

The DB also has a `UNIQUE(market_id) WHERE order_status = 'OPEN'` partial
index as a secondary guard. Both layers are required.

---

## COMMON MISTAKES — EXPLICITLY CODED AGAINST

The following are documented failure modes. Every item below must have either
a unit test proving it cannot happen, or a code comment explaining the guard.

**MISTAKE 1 — VOLUME DOUBLE COUNTING**
Summing all `OrderFilled` events = 2× real volume. Result: bot thinks markets
are liquid when they're not. Position sizing and close orders both break.
Guard: taker-side only filter in `gamma_client.py` + unit test.

**MISTAKE 2 — GTC ORDERS LEFT ON BOOK**
A GTC "No" order at $0.16 will be sniped by HFT bots if news breaks and
"No" gaps to $0.05. The bot just sold "No" for $0.16 when it's worth $0.95.
Guard: FOK orders only, enforced in `clob_client.py` with an assertion that
raises if order_type != "FOK".

**MISTAKE 3 — SHARES CALCULATION ERROR**
`shares = size` ignores price entirely. At $0.15 entry, $15 USDC buys 100
shares, not 15. Wrong shares means wrong take-profit and stop-loss triggers.
Guard: unit test asserts `shares * entry_price ≈ size_usdc`.

**MISTAKE 4 — ACTING ON SINGLE-CYCLE PRICE SPIKES**
One large buy order pumps "Yes" to 85% for a single poll cycle, then reverts
to 65%. Bot fires on noise. Guard: 2-consecutive-cycle confirmation in
`scanner.py` with `last_seen_above_threshold` tracked in DB.

**MISTAKE 5 — NO KILL SWITCH ON WS DISCONNECT**
Bot is blind to market moves with resting orders still open. Kill switch
fires on 90s WS silence and cancels all orders via REST.

**MISTAKE 6 — NOT ACCOUNTING FOR FEES IN EV**
2% fee on net winnings makes low-multiplier trades net-negative.
Guard: fee-adjusted EV check in `validator.py` with minimum ratio enforced.

**MISTAKE 7 — ASSUMING FILL FROM ORDER PLACEMENT**
HTTP 200 from order placement means the order was accepted, not filled.
Guard: always confirm fill status via CLOB order status endpoint or user
WebSocket channel before updating `order_status` to OPEN.

**MISTAKE 8 — RACE CONDITION ON POSITION WRITES**
Two async tasks open duplicate positions for the same market.
Guard: `asyncio.Lock()` per market_id + DB unique partial index.

**MISTAKE 9 — LEAKED AIOHTTP SESSIONS / DB CONNECTIONS**
Unclosed connections cause memory growth over hours on VPS.
Guard: all sessions opened via `async with`, all DB connections closed in
`finally` blocks. Memory profile test required before live deployment.

**MISTAKE 10 — HARDCODED STRATEGY PARAMETERS**
If 80% is no longer the right threshold, you need a code deploy to change it.
Guard: every parameter in `.env` via `config.py`. Zero hardcoded values.

**MISTAKE 11 — NO DAILY LOSS LIMIT**
A bug or bad run drains capital with no operator watching.
Guard: `MAX_DAILY_LOSS_USDC` enforced in `limits.py`, checked before every
order AND in the kill switch monitor loop.

**MISTAKE 12 — TRUSTING API SUCCESS RESPONSES WITHOUT VERIFYING STATE**
The CLOB API may return 200 for an order that is queued but not yet processed.
Guard: poll order status endpoint after placement until status is confirmed
`MATCHED` or `CANCELLED`. Add a 15-second timeout with kill switch fallback.

---

## QUESTIONS TO ASK THE USER BEFORE ANY CODE IS WRITTEN

Do not write a single line of code until all of the following are answered:

**Q1.** What wallet type? (Standalone EOA / Polymarket Proxy / Gnosis Safe)
This determines the signing method and whether gasless relayer is available.

**Q2.** Which market categories should be whitelisted? (Sports, Politics,
Crypto, Finance, Weather, Entertainment, etc.) Which must be excluded?

**Q3.** Starting capital in USDC? Needed to validate that `POSITION_SIZE_USDC`
and `MAX_TOTAL_EXPOSURE_USDC` are sensible relative to total capital.

**Q4.** Should the bot run in paper-trade mode first (simulated orders, no real
execution) with a config flag to switch to live? Strongly recommended.

**Q5.** VPS or local machine? If VPS: region, OS, Python version available.

**Q6.** Notification method when kill switch fires?
(Telegram bot / email / terminal log only)

**Q7.** Should markets within 24h of a known scheduled catalyst (Fed meeting,
election day, earnings) be skipped to avoid news-driven spikes that are NOT
pure optimism bias? If yes, how should the bot detect these windows?

---

## BUILD ORDER — FOLLOW EXACTLY, NO SKIPPING PHASES

### Phase 1 — Foundation
```
[v0.1.0] DB schema + migration runner
[v0.1.1] Config loading + .env validation (fail loudly on missing keys)
[v0.1.2] Logging setup (structlog, rotation, JSON format verified)
[v0.1.3] All foundation tests passing
```

### Phase 2 — Authentication
```
[v0.2.0] L1 EIP-712 signing module + unit test
[v0.2.1] L2 HMAC-SHA256 headers + unit test
[v0.2.2] Server time sync on startup (clock drift guard)
[v0.2.3] All auth tests passing
```

### Phase 3 — Data Layer
```
[v0.3.0] Gamma API client (poll, parse, taker-side volume filter)
[v0.3.1] Volume double-counting unit test — explicitly prove single-sided
[v0.3.2] CLOB API client (orderbook fetch, FOK order placement, cancellation)
[v0.3.3] WebSocket client (connect, subscribe, reconnect with backoff)
[v0.3.4] Token bucket rate limiter (both buckets, async-safe)
[v0.3.5] All data layer tests passing
```

### Phase 4 — Strategy Core
```
[v0.4.0] Scanner (poll + threshold detection + 2-cycle confirmation)
[v0.4.1] Validator (all 9 checks, each individually unit-testable)
[v0.4.2] Shares calculation + fee-adjusted EV calculation with unit tests
[v0.4.3] Decision module (full signal → validation → order flow, all branches)
[v0.4.4] Position manager (open, monitor, close — all edge cases covered)
[v0.4.5] All strategy tests passing
```

### Phase 5 — Risk & Safety
```
[v0.5.0] Kill switch (all 5 triggers, cancel-all, graceful shutdown sequence)
[v0.5.1] Health monitor (WS heartbeat, automatic kill trigger at 90s)
[v0.5.2] Daily loss limit enforcement + exposure cap
[v0.5.3] asyncio.Lock() race condition protection on all position writes
[v0.5.4] All risk tests passing — kill switch mock test is mandatory
```

### Phase 6 — Paper Trading Mode
```
[v0.6.0] PAPER_TRADE=true flag disables real order placement
[v0.6.1] Simulated fills based on current orderbook mid price
[v0.6.2] Paper P&L tracked separately in DB (paper_positions table)
[v0.6.3] Run paper mode for minimum 72 hours before enabling live
```

### Phase 7 — Live Deployment
```
[v0.7.0] Live mode enabled only after user confirms capital amount
[v0.7.1] First live trade logged and manually verified by operator
[v0.7.2] 24-hour live run checkpoint review
```

---

## PRE-LIVE CHECKLIST

Every item must be explicitly verified before `PAPER_TRADE=false` is set:

- [ ] Private key is never logged, printed, or stored in DB at any point
- [ ] `.env` is in `.gitignore` and has not been committed
- [ ] Volume is taker-side only — unit test passes
- [ ] Shares calculation unit test passes
- [ ] Fee-adjusted EV unit test passes
- [ ] FOK order type is the only order type used — assertion in clob_client.py
- [ ] Kill switch tested with mock CLOB — cancel-all fires before any shutdown logic
- [ ] WebSocket disconnect fires kill switch — tested
- [ ] Daily loss limit halts trading at threshold — tested
- [ ] Race condition lock tested — no duplicate OPEN positions for same market_id
- [ ] DB unique partial index on open positions confirmed
- [ ] Paper mode runs without errors or memory growth for 72 hours
- [ ] All phases 1–5 tests marked PASS at current version
- [ ] Sonar memory updated with full architecture summary and known edge cases
- [ ] `python main.py --kill` tested and confirmed to cancel all orders cleanly
