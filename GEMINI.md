# 🧠 Cognitive & Logical Verification Guardrails

To prevent logical blind spots, tunnel vision, and shallow assumptions, the AI assistant MUST strictly apply these system-level thinking principles to every design, audit, and implementation step.

## 1. The Skepticism Rule (Assume Your First Idea Is Wrong)
* The agent is prohibited from presenting a single, neat conclusion or edge without actively trying to disprove it first.
* For every proposed change, optimization, or strategic conclusion, the agent MUST explicitly write down:
  1. Two distinct failure modes (how this can fail under live market conditions).
  2. The trade-offs involved (what is being sacrificed, such as latency, fee drag, or opportunity cost).

## 2. System-Level Correlation Rule (No Vacuums)
* The agent is forbidden from optimizing any single component, profile, or function in a vacuum.
* Before modifying or proposing an edge for any strategy, the agent MUST trace and verify:
  * **Capital Overlap**: Does this profile trigger on the same markets as other profiles?
  * **Opposing Legs**: Is this trade part of a correlated pair, hedge, or concurrent recovery loop?
  * **Execution Friction**: Does the proposed logic survive real-world transaction fees, spreads, and execution slippage?

## 3. Mandatory Context Trace (Anti-Historical Erasure)
* Before refactoring or changing any formula, parameter, or logic that seems "redundant" or "wrong" (e.g., nested multipliers or price floors), the agent MUST trace back to its origin in the codebase and git history.
* The agent must verify if the code is a deliberate safety guardrail (such as a defensive Kelly sizer multiplier or price approach Direction Gate) designed to prevent capital ruin. Do not delete safety logic under the guise of "cleaning up."

## 4. Real-World Constraint Modeling (No Idealized Systems)
* The agent must always assume the trading environment is hostile, illiquid, and high-latency. 
* Any logic, backtest, or position monitor code MUST explicitly model real-world constraints:
  * Order books can have wide spreads.
  * Bid liquidity collapses to zero at wicks and expirations.
  * API endpoints can experience network latency and rate limits.
  * WebSocket feeds can silently drop ticks (zombie streams).

## 5. Statistical Significance Gate (Anti-Noise Rule)
* The agent is banned from using qualitative heuristics, logical hand-waving, or theoretical assertions to justify strategy parameters. 
* Every quantitative parameters claim must be backed by an empirical database query run across the entire historical dataset, with at least 100 sample events, or reproducing unit tests. If the data is not there, the agent must state: "We have insufficient data to conclude this."

# ♊ Gemini Project Instructions: Polymarket ReverseBot (RuFlo V3)

This project is a high-performance algorithmic trading bot designed for Polymarket's crypto micro-markets (5-minute and 15-minute intervals).

- NOTE: For your workings;
 1. **Ask the user about the decision you are talking or any questions you have in mind each time .. you want to do something which was asked .. 
 2. **Correct the user if you feel that is important rather than taking a wrong decision..
 3. **Mandatory Graphify Usage**: You are REQUIRED to use the `graphify` MCP server tools (specifically `query_graph`, `get_node`, `get_neighbors`, `get_community`, `shortest_path`) as your primary method to navigate, search, and understand the codebase before reading raw files or doing expensive full-text greps, in order to conserve context window token budget.
 4. **Mandatory Exit Scope Clarification**: Before implementing, simulating, or auditing exit profiles/strategies (such as Trailing Take Profit (TTP), Static Take Profit (TP), or Stop Loss (SL)), you MUST explicitly ask and clarify with the user whether these exits apply to the virtual simulated profiles (`stop_loss`, `loss_skip`, etc.), the core paper/live trading bot, or both. Never assume virtual profiles support these exits by default or mirror the core bot features without prior codebase verification and explicit confirmation.
 5. **Polymarket CLOB WebSocket Zombie Stream & Fallback**: The WebSocket connection can stay open and heartbeats succeed while specific market streams silently stop receiving ticks. Storing a local LKG price fallback creates a blind spot where stop losses are disabled. To fix this, always utilize the throttled REST API fallback `_get_current_price_with_rest_fallback` (10s cooldown per token ID) in the position monitoring loops to fetch fresh prices directly from the CLOB when WebSocket caches are empty. *Operational Note*: Be aware of the **First-Tick Fallback Blind Spot** on brand-new positions. If the WebSocket cache is empty, the REST fallback is throttled or returns `None` (e.g. if orderbook has no bids), and `self._lkgp_memory` does not yet contain a price, the position monitoring loop will skip stop loss checking entirely.
## 🚀 Project Overview
- **Purpose**: Automates trading on Polymarket "Up or Down" micro-markets using a streak momentum strategy.
- **Core Technology**: Python, `asyncio`, SQLite, Polymarket (Gamma & CLOB APIs), and Binance Klines API.
- **Architecture**:
    - `src/api/`: Integration with Polymarket and Binance.
    - `src/strategy/`: Strategy logic (Streak detection, Decision, Validation, Position Management).
    - `src/risk/`: Circuit breakers, exposure limits, and emergency kill switch.
    - `src/db/`: SQLite persistence layer.
    - `R_and_D/backtesting/`: Production-grade backtesting engine using Binance historical data.
    - `PROFILES.md`: Authoritative technical specification & empirical research registry for strategy profiles (e.g. `micro_flow_no`).

## 🛠️ Building and Running
- **Environment**: Use the local virtual environment: `source venv_local/bin/activate`.
- **Testing**: Run all unit tests before deployment: `./venv_local/bin/pytest`.
- **Execution**: 
    - Local (Paper Trade): `python src/main.py --env .env.rd --paper`
    - Remote (VPS): Deployment via `rsync` and process management via `pm2` ip addresss of the vps is 68.210.96.175 passwordless SSH key is configured with connection pooling .the project is in ubuntu/reversebot/
- **Configuration**:  and `.env.rd` (R&D/Paper).

#
- **CRITICAL RULE - There is no stop loss or exit execution in the bot core logic as the bot wait only for the resolution resutls either via oracle where is lazy and delayed always or with binance kline api .. 
## 📜 Development Conventions
- **Asynchronous**: All I/O and trading loops must use `asyncio`.
- **Logging**: High-signal JSON logging. Use `logger.info("event_name", **payload)` for structured tracking.
- **Verification**: New features must include a corresponding test in the `tests/` directory.
- **Strategic Advice & Constraints**: NEVER propose, recommend, or apply quantitative parameters (e.g., Stop Losses, position sizing adjustments, indicator thresholds) based on raw heuristics or logical assumptions. All strategic advice MUST be mathematically verified against actual PnL simulations first. Furthermore, do not state strategic changes as facts or implement them without explicit user confirmation.
- **No Theoretical Assertions (Empirical-Only Rule)**: The AI coding assistant MUST NOT make qualitative or quantitative claims about strategy safety, timeframe edges, price floors, or indicator validity using logical assumptions or theoretical heuristics. Before giving any strategic recommendation or asserting that a timeframe or parameter is "safe," the agent is REQUIRED to write and run a diagnostic query script against the active VPS database (`reversebot_rd.db`) to verify the claim with actual trade history, PnL, win rates, and sample sizes.
- **Safety First**: Use the `KillSwitch` and `RiskLimits` to prevent catastrophic capital loss.
- **Streak Reset**: Once a trade is taken, the bot must wait for a fresh, non-overlapping streak to form before re-entering (to avoid "Momentum Exhaustion Traps").
# Project: Reversebot (Local Mac & Remote VPS Environments)

## Critical Environment Constraints
- **Local Dev:** macOS (MacBook Air), Shell: zsh. Package manager: Homebrew / uv.
- **Production VPS:** Linux (Ubuntu/Debian standard), Shell: bash.
- **Command Safety Rule:** Before running ANY terminal command targeting a VPS deployment, verify tool compatibility. 
- *Example:* Never use macOS `sed -i ''` syntax on the Linux VPS. Use the standard Linux `sed -i` configuration instead.
- **Commands you can avoid "git diff"

## Self-Correction & Reflection Cycle
If a terminal command you execute results in an error (e.g., `command not found`, execution crashes, deployment script failures):
1. **Analyze the root cause:** Identify why the environment rejected the command.
2. **Update Workspace Memory:** You are REQUIRED to immediately open, edit, and append the solution directly into `.agents/skills/self-heal/failures.log`.
3. **Fix the Core Context:** If the failure points to a structural misunderstanding of the codebase or VPS architecture, rewrite the matching section of this `GEMINI.md` file using your file-editing tools.

## Mandatory Documentation Standards
Every time you modify a file (`GEMINI.md`, rules, workflows, or codebase scripts) to resolve an environmental issue, you must write a highly detailed audit entry to `CHANGELOG.md` using this layout:
- **Timestamp:** Current Date/Time
- **Action Taken:** What file was modified.
- **The Problem:** The exact error log or failed command.
- **The Root Cause:** Why it failed (e.g., "macOS vs. Linux string-matching syntax differences").
- **The Solution:** What changes were introduced and why they ensure flawless execution moving forward.
## Remote VPS Server Constraints (68.210.96.175)
- **SSH Username Constraint**: Always use the username `ubuntu` (not `root`) when performing `ssh` or `rsync` operations against the VPS server.
- **Nginx Web Server Auto-Start & Dual Routing Guard (v3.120)**: When auditing VPS status after server reboots, always verify that `nginx.service` is `enabled` and `active` (`sudo systemctl enable --now nginx`). Furthermore, ensure `/etc/nginx/sites-available/reversebot` maps both root `/` and `/rd/` routes to the active FastAPI dashboard port (3001) so web traffic is never rejected or 404'd when accessing the server root IP.
- **Known Issue:** The `sqlite3` CLI tool is NOT installed globally on the VPS. 
- **Command Rule:** Never attempt to run raw `sqlite3` commands directly over SSH on the VPS without either:
  1. Verifying or installing the package first (`sudo apt-get install sqlite3 -y`).
  2. Or executing the query programmatically via a Python script already running in the `uv` environment on the server.
- **Current Database Path:** `~/reversebot/data/reversebot_rd.db`
- **VPS Swap Memory**: 2GB swap memory (`/swapfile`) is active and persistently configured in `/etc/fstab` on VPS `68.210.96.175` to prevent OOM process kills.
- **NEVER COPY DATABASE RULE:** Never copy the database from the remote VPS server to the local machine. Instead, always create the query or analysis script locally, transfer it to the VPS (e.g. via `rsync` or `scp`), and execute it directly on the VPS. If there is a mismatch of Python versions, verify that the remote execution environment on the VPS is set up to run using the same Python environment/version (e.g. `uv run` or using the local active virtual environment's interpreter path).
- **Crucial Sync Mismatch Guard:** The `.env` and `.env.rd` configuration files are excluded by default in our standard `rsync` deployment rules to prevent overwriting private keys. This means any local updates to `BOT_VERSION` or other strategy constants inside `.env.rd` must be manually modified on the remote VPS server, followed immediately by a PM2 restart (`pm2 restart reversebot_rd`), to prevent silent version mismatch loops.
- **Self-Healing Stuck Positions Rule:** When Polymarket Daily/Weekly crypto markets expire, they settle at extreme fractional wicks like `0.9995` / `0.0005` on the Gamma API, but their official settlement state stays `closed: False` and `active: True` for hours/days awaiting oracle clearance. 
  To prevent positions getting stuck in `resolving` indefinitely:
  - Do NOT check for strict `p_target in (0.0, 1.0)`.
  - Use `p_target >= 0.98` (win, payout 1.0) and `p_target <= 0.02` or `p_opposing >= 0.98` (loss, payout 0.0) as the self-healing resolution thresholds once the market's `closes_at` has passed. This closes paper trading positions instantly and updates downstream telemetry.
- **SQLite Database Lock (database is locked - 5) Strict Protocol & Self-Healing Rules:** 
  1. **Strict Prevention Protocol for Ad-Hoc / Scratch Scripts**:
     - **MANDATORY Context Managers**: Bare connections like `db = await aiosqlite.connect(...)` or `conn = sqlite3.connect(...)` without `with` / `async with` or `try ... finally: conn.close()` are STRICTLY FORBIDDEN in any scratch script, diagnostic, or one-liner. Every connection must guarantee clean termination upon script completion or error.
     - **MANDATORY Read-Only Mode for Audits**: Any diagnostic, audit, backtest, or exploration script that only executes `SELECT` queries MUST connect using SQLite's URI read-only mode:
       `conn = sqlite3.connect("file:" + os.path.expanduser("~/reversebot/data/reversebot_rd.db") + "?mode=ro", uri=True)`
       Connecting with `mode=ro` prevents the script from acquiring POSIX write/advisory locks or pinning the WAL read mark that blocks WAL auto-checkpointing.
     - **MANDATORY Busy Timeout**: All connections in any script MUST specify `timeout=60.0` and execute `PRAGMA busy_timeout = 60000;` immediately upon connecting.
     - **FORBIDDEN Detached Background Execution Without Timeout**: Never run scratch scripts in the background (`nohup`, `&`, or unmonitored subshells) without a strict OS timeout (`timeout 120 python scratch/script.py`).
     - **Post-Execution Process Sweep**: After running any diagnostic on the VPS, verify that no hung processes remain: `ps aux | grep scratch | grep -v grep`. If any lingering processes exist, terminate them immediately.
  2. **Self-Healing Incident Resolution Sequence**:
     - If a database operation fails with `database is locked` on the remote VPS:
       1. Identify all processes holding open handles: `lsof ~/reversebot/data/reversebot_rd.db`.
       2. Terminate any orphan or non-PM2 holding PIDs: `kill -9 <PID>`.
       3. Restart the main trading engine via PM2 (`pm2 restart reversebot_rd`) to reset any internal connection deadlocks caused during the lock freeze.
       4. Run a passive WAL checkpoint (`PRAGMA wal_checkpoint(PASSIVE);`) to flush pending WAL frames cleanly into the main database.
- **SQLite Join Optimization (Non-Indexed Text Keys) Rule:** When joining large tables (like signals and profile_positions) in SQLite on non-indexed text keys (like market_id), queries can hang or block due to nested full-table scans. To prevent execution timeouts and database lockups:
  1. Do not use direct SQL joins (`LEFT JOIN`, `INNER JOIN`) on text columns if they lack matching indexes.
  2. Instead, load the required rows into memory using simple queries.
  3. Perform the matching programmatically in Python using dictionaries, which executes instantly.

- **Kelly Sizer Mathematics & Double Multiplier (Quarter-Kelly) Rule:** 
  1. **Quarter-Kelly Sizing**: The bot intentionally applies two sequential `0.5x` multipliers to prevent capital ruin. The stored `kelly_fraction` in `win_rate_tracker` is Half-Kelly, and the live sizing engine in `decision.py` applies another `0.5x` global defensive multiplier on top of the calculated live Kelly fraction, resulting in an effective bet size of **Quarter-Kelly** ($\text{Full Kelly} \times 0.5 \times 0.5$). Do NOT remove either of these multipliers thinking it is a redundant bug.
  2. **Live Odds $b$ Re-computation**: To prevent over-betting on expensive contracts (e.g. entries near $0.58$), the sizer must *always* recompute the odds parameter $b$ at signal time using the live entry price (`entry_price`) offered by the CLOB and the historical rolling win rate $p$, rather than using the pre-computed average database reference Kelly.
  3. **Friction-Adjusted Odds**: The odds parameter $b$ must incorporate simulated 2% transaction friction fees/slippage. The correct net odds formula is:
     $$b = \frac{0.98}{\text{entry\_price}} - 1.0$$
     This formula must be used consistently in both the offline database tracker recalculation (`queries.py`) and the live sizing engine (`decision.py`).
  4. **Purged Reversal Sizers**: Martingale and D'Alembert loss recovery/algebraic recovery sizers are completely purged from the codebase. All trades cleanly execute using the live dynamic Quarter-Kelly sizing engine.
  5. **Streak Sizing Multipliers (v3.94)**: Streak multipliers are scaled based strictly on streak depth:
     - **BTC 5m**: Depth 2 = `1.5x`, Depth 3 = `1.0x`, Depth 4 = `1.0x`, Depth 5+ = `0.3x`
     - **All others**: Depth 2 = `1.3x`, Depth 3 = `1.0x`, Depth 4 = `0.5x`, Depth 5+ = `0.3x`

- **Price Approach Direction Gate (Falling Knife Guard) Rule:**
  1. **The Momentum Reversal Trap**: In trend-following strategies, buying pullbacks represents buying pullbacks on trend exhaustion. For example, if a YES contract price has risen above $\$0.65$ (Gate 5 ceiling) and then drops back down to $\$0.64$, buying YES in this scenario is catching a "falling knife" (momentum is heading *down*, not *up*).
  2. **Band-Pass Approach Calibration**: To prevent the adverse selection trap without incurring massive opportunity costs on high-probability reversion trades, the gate must be configured as a **Band-Pass Filter** rather than a flat threshold:
     - **Lower Band Threshold (3.0¢)**: Minor micro-wiggles and normal spread noise under 3.0¢ (magnitude `< 0.030`) must be **allowed** (not rejected) as they represent normal breakout volatility and offer highly profitable entry points.
     - **Toxic Pullback Block Zone (3.0¢ to 6.0¢)**: True momentum exhaustion drifts (magnitude `>= 0.030` and `< 0.060`) must be **rejected** (blocked) as they are statistically prone to net losses (Friend rate = 52.2%).
     - **Reversion Wick Overreaction Pass (>6.0¢)**: Deep, severe dumps and extreme volatility wicks (magnitude `>= 0.060`) must be **allowed** (not rejected) because they represent short-term orderbook mispricings that quickly snap back in the streak direction, offering a high-payout deep discount (Enemy rate = 56.8%).
  3. **Zero Additional Latency**: This gate utilizes the existing 1-second resolution `spread_logs` table. No new API calls are made at evaluation time.
  4. **Telemetry Preservation**: The exact calculated price velocity is logged into the `price_velocity` field of SQLite `signals` evaluations, facilitating high-fidelity post-execution auditing.

- **Strategic Guard Soft-Disabling & YES Asymmetry Rules (v3.34-streak-bypass-guards):**
  1. **Re-enabled Hard Guards**: The **B3 Price Gate** remains an active hard rejection block in `decision.py`. It requires the underlying YES contract price (`yes_price`) to be strictly in the range `[0.30, 0.60]` for both YES and NO entries. This restricts YES entry prices to `[0.30, 0.60]` and NO entry prices to `[0.40, 0.70]`.
  2. **Soft-Disabled Guards**: The **Order Book Imbalance YES Sellers Dominate Guard** (`book_imbalance < -0.15` when `side == "YES"`) and the **Order Book Imbalance NO Buyers Dominate Guard** (`book_imbalance > 0.15` when `side == "NO"`) are soft-disabled to allow execution but log evaluation warnings and store a `"WOULD_REJECT: ..."` trace in the database `guard_rejection_reason` column for opportunity-cost audits. *Audit Note (v3.27)*: The CVD NO Guard was empirically audited and optimized separately for timeframes: disabled for 5m markets to capture high-edge blow-off top mean-reversions, and calibrated to a **$100,000.0 USD threshold** for 15m markets, which blocks only the most toxic institutional wicks (historical win rate = **33.82%**) and raises the 15m Passed Pool win rate to **61.60%**.`
  3. **Hard-Enabled Guards**: The **LOSS_FADE Concurrency Guard** (which blocks concurrent recovery trades when another recovery trade is active or unresolved skips exist) is hard-enabled as a critical barrier to prevent concurrent recovery trade losses.
  4. **Disabled YES Asymmetry Prioritization (A4)**: YES signals prioritization sorting is disabled/purged. All scanner signals are evaluated neutrally based on sequence (or EV/quality score) without side-based sorting.
  5. **Dynamic Kelly Sizer Active**: Dynamic sizing uses the live Quarter-Kelly sizer capped symmetrically by the base config value. currently kelly is soft disabeled .. 
  6. **Disabled Asset Cap / Similarity Guard (v3.25)**: The Similarity Guard / Asset Cap check ("asset_cap_rejected" / "asset_correlation_cap") inside process_signal is completely disabled/bypassed for short-window crypto markets. This allows concurrent trades on the same core asset (e.g. trading both BTC 5m and BTC 15m contracts concurrently) to maximize signal capture and prevent lockout of high-probability setups. It continues to log "asset_cap_bypassed" at info level for opportunity auditing but does not block order execution.
  7. **Purged Reversal Streak Bypass**: The reversal streak guard bypass is completely deleted. All signals and trades strictly respect B3 price ceiling/floor gates, CVD confirmation thresholds, Falling Knife Price Approach checks, and EV requirements.
- **Web Dashboard Template Distinction Rule (v3.39)**:
  - **Active Web Directory**: The FastAPI server serves HTML templates from the root `dashboard/` directory (mapped via `dashboard_dir = Path(__file__).parent.parent.parent / "dashboard"`).
  - **Legacy Templates**: The files located at `src/api/index.html` and `src/api/rd.html` are legacy, non-functional duplicates and should NOT be modified or relied upon for UI updates. Always edit the files inside the `dashboard/` directory directly.
  - **Dynamic Routing for rd.html**: The R&D template `rd.html` is used as the default dashboard for the short-window crypto market mode. To prevent live runs from being locked to paper trading tables, API fetches and WebSockets in `rd.html` dynamically adapt their prefix (`/api/` and `/ws` vs `/rd/api/` and `/rd/ws`) based on the URL pathname, allowing it to correctly pull live stats and resolved trades when loaded at the root route `/`.

- **Web Dashboard Layout & Persistency Filtering Rules (v3.119)**:
  - **Table Layout**: In the Trades Audit Log, the raw columns "Opened Time" and "Closed Time" are removed to simplify the table layout. They are replaced by a "Debug Info" column (housing the Position ID and Close Reason) and a "Persist" column (showing the persistence score at signal time). Full opened/closed timestamps (both in Eastern Time and Indian Standard Time) are relocated to the expandable details accordion grid for clean rendering.
  - **Persistency Filtering**: The backend filtered history endpoint (`/api/history-filtered` and `/rd/api/history-filtered`) accepts `persistency_mode`, `persistency_op`, and `persistency_val` to perform query-time subset filtering. In the frontend, the persistency selector supports pre-set options (`< 55%`, `>= 55%`) and a toggleable custom input filter allowing manual operator/value selections.


- **Kline Resolution Timeframe Rule (v3.41)**:
  - **timeframe Extraction**: During position resolution checks (`check_resolutions` in `position_mgr.py`), always extract `window_minutes` directly from the position record (`pos.get("window_minutes")`), not from the joined market metadata dictionary. The `markets` database table lacks a `window_minutes` column, so referencing it results in a `None` value that silently defaults contract evaluations to 5-minute intervals.

- **Polymarket Deposit Proxy Wallet Balance & Token Rules (v3.42)**:
  1. **Contract Proxy vs. EOA Address**: Polymarket accounts store active trading funds inside a smart contract proxy deposit wallet (e.g. `0x0bd52479b8aabdd46d7b6154c1880828e4835d49`), whereas trades are signed via the EOA wallet (e.g. `0x79Fc617a7a0691A31FA4D2869aC3A8140f4429F0`). Always verify balances against the smart contract proxy address.
  2. **Collateral Token**: The active collateral token for Polymarket CLOB V2 is **Polymarket pUSD** (contract `0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb`), which replaces legacy USDC.e. Standard on-chain checks should query pUSD balances directly.
  3. **Relayer API Keys & HMAC Auth**: Relayer API Keys (e.g. `019c55da-...`) generated under the Relayer tab require their own matching L2 API Secret and Passphrase to authenticate L2 REST queries (like `get_account_balance`). Mixing a new Relayer API Key with old secrets/passphrases results in a `401 Unauthorized` response.

- **Polymarket CLOB API Order Status Casing Rule (v3.46)**:
  Always cast status strings returned by `clob.get_order_status` to uppercase (e.g., `str(status).upper()`) before comparing to `"MATCHED"` or `"FILLED"`. The live Polymarket CLOB API returns lowercase order status strings (like `"matched"`), which causes strict uppercase equality checks to fail silently, resulting in zombie positions in the database.

- **Mock get_synced_time() Sync vs Async Guard Rule (v3.51)**:
  In unit tests, the CLOB client is often replaced by an `AsyncMock()`. Because `AsyncMock` dynamically wraps all attribute lookups and methods as asynchronous coroutines, calling the synchronous `get_synced_time()` on a mocked CLOB yields an awaitable coroutine object instead of an integer/float timestamp. To prevent `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'coroutine'` during execution:
  - Do NOT call `self.clob.get_synced_time()` directly in components (`decision.py`, `validator.py`, `scanner.py`, `position_mgr.py`).
  - Always route timestamp requests through a local `_get_now()` helper that inspects the returned value, checks if it is a coroutine or has `__await__`, and falls back to standard `time.time()` if so.

- **Mock Config Object Attribute Lookup Truthiness Trap Guard (v3.66)**:
  In unit tests, the config object is often mocked using `MagicMock()`. Because Python's `MagicMock` dynamically returns a truthy sub-mock object for any attribute lookup not explicitly defined (e.g., `getattr(self.config, "concurrency_limit_test_override", False)` returns `<MagicMock ...>`), logical negation checks like `not getattr(self.config, "override", False)` will evaluate to `False` instead of `True`, silently bypassing safety rules. To prevent this silent mock bypass:
  - Never use simple truthiness checks on dynamic config variables that might be mocked.
  - Always perform explicit type-safe identity checks: `getattr(self.config, "override", False) is True`.

- **Mocked DB/Objects Await TypeError Guard (v4.78)**:
  In unit tests, objects like the position manager (`pos_mgr`) are mocked using `MagicMock`. Retrieving nested attributes from them (e.g., `getattr(self.position_mgr, "skip_db", None)`) returns a new `MagicMock` object rather than `None`. Attempting to call and await methods on this mock (e.g., `await skip_db.get_last_closed_skipped_position()`) raises `TypeError: object MagicMock can't be used in 'await' expression`. To prevent this await TypeError crash:
  - Always verify if the retrieved object is a mock before using/awaiting its methods:
    `from unittest.mock import MagicMock, AsyncMock`
    `if isinstance(obj, (MagicMock, AsyncMock)): obj = None`

- **Option A Pure Trend-Following Strategy & Global FAK Execution (v3.75)**:
  1. **Streak 2+ Fully Active**: Standard Streak 2 and Streak 3+ trend-following signals are fully active. Dynamic sizing uses the live Quarter-Kelly sizer capped symmetrically by the base config value.
  2. **Non-Blocking Spread & Depth Guards**: Spread multiplier and orderbook depth checks are completely non-blocking (info-only telemetry). They run to write rejection events and warning logs for analytics/dashboard auditing, but never reject signal entry.
  3. **Global FAK Execution & Retry Cascades**: Every single trade (standard trend-following or deep wicks) executes using FAK initial orders, multi-strike slippage retry cascades , minimum 5.0 share clamping, and VWAP database updates. Legacy FOK order paths on entry are completely purged.
  4. **Purged Reversal dead-weight**: The D'Alembert linear reversal recovery sizers, look-ahead shields, 20% margin algebraic recovery boosts, late-entry target profit scaling, and single-reversal concurrency limits are completely purged. All trades cleanly fall through to the live dynamic Quarter-Kelly sizing engine.
  5. **B3 Price Gates Fully Active**: B3 price gates remain active hard barriers for standard signals, requiring the underlying YES price (`yes_price`) to be in `[0.30, 0.60]` for both YES and NO entries (protecting capital from adverse late momentum). B2 seller/buyer imbalances are soft-disabled (log warnings and opportunity audit tags, but proceed with entry).



- **Execution over SSH Debugging Tip**: Do not attempt to run complex python inline one-liner scripts with `-c` containing single quotes nested in double quotes via SSH. This triggers syntax errors in python or bash parser mismatches. Instead, write a proper python script under `scratch/` locally, use `rsync` to transfer it to the VPS, and execute the file programmatically.

- **Position Manager Cascade Fills & Price Gates Rule (v3.90)**:
  During cascading FAK fill loops (e.g. `open_position` in `position_mgr.py`), pre-flight empirical price gates (B3 ceilings/floors) must ONLY be enforced on retry attempts (i.e. `attempts > 1`). Enforcing them on the first attempt will cause unit tests (which bypass signal validation and call `open_position` directly with mock prices) to fail immediately.

- **Direction-Aware Persistence Score & Regime Tracker Rule (v4.42)**:
  1. **Conditional Probability Calculation**: Persistence scores are tracked as conditional continuation probabilities: $P(\text{UP} \mid \text{prev}=\text{UP})$ and $P(\text{DOWN} \mid \text{prev}=\text{DOWN})$ separately rather than a single overall continuation score. The overall persistence score can also be queried.
  2. **Doji Bias Prevention**: Candles are categorized using strict greater-than (`close_price > open_price`) for UP, meaning doji/flat candles (`close_price == open_price`) are classified as DOWN to avoid positive bias.
  3. **Responsive Lookback Window**: The lookback window is exactly 30 closed candles (meaning 29 transitions are evaluated) for responsiveness to changing regimes.
  4. **On-Demand Refresh**: The decision engine triggers an async `refresh_score(asset)` to pull the newest Binance klines before pre-trade evaluations, ensuring scores are never stale.
  5. **DB Recalculation & Telemetry**: Evaluated scores and counts are saved in `data/regime_telemetry.db` under the `persistence_history` table containing columns: `p_up_continues`, `p_down_continues`, `up_continuations`, `down_continuations`, `total_after_up`, `total_after_down`. Total transitions in unit tests must be checked as `total_after_up + total_after_down` (which equals 29/30 depending on lookback).
  6. **UI Format**: The Web Socket payload broadcasts a dictionary of stats `{"up": ..., "down": ..., "overall": ...}` for each asset. The frontend updates UI elements separately showing `UP↑` and `DOWN↓` scores.
  7. **Shadow Mode / Soft Warning**: The guard runs in shadow/soft warning mode where low persistence scores do not block order entry, but append warning logs and soft audit rejections to the database.

- **Option B1 Reversal Toggling Rule (v4.46)**:
  1. **Toggling Criterion**: The bot automatically tracks the rolling win rate of the last $N=10$ completed trades (where `order_status = 'FILLED'` and `realised_pnl IS NOT NULL`). If this win rate falls below 45% (switch threshold), the bot toggles between `TREND` and `REVERSAL` mode. If win rate is $\ge 45\%$, it retains the mode of the last completed position.
  2. **Reversal Execution (Fade)**: In `REVERSAL` mode, the bot fades the momentum streak direction: flips side (YES $\leftrightarrow$ NO) and `target_token_id` (opposing token ID), and applies a 1¢ `reversal_spread_penalty` to the opposing entry price: `entry_price = 1.0 - trend_price + reversal_spread_penalty`.
  3. **Stateless Notes JSON Persistence**: To survive restarts statelessly, the strategy mode and token ID are saved as a JSON payload in the database `notes` column: `{"tid": target_token_id, "strategy": "TREND"|"REVERSAL"}`.
  4. **Token Healing Note Preservation**: In `_recover_target_token_id`, notes are updated to update healed token IDs while preserving the JSON format and `"strategy"` field if it exists.
  5. **Mock Safeguard**: To prevent `sqlite3.InterfaceError` and mock-based syntax errors in unit tests, all numerical lookups and mock config checks are guarded to ensure MagicMock/AsyncMock objects are cleanly filtered out or fall back to defaults.

- **Independent Strategy Profiles & Purged Guards Rule (v4.94)**:
  1. **Independent Profile Evaluation**: All virtual strategy profiles (including `loss_skip` and `streak_momentum`) must be evaluated early in `process_signal` immediately after `validation.passed` is verified. This ensures virtual profiles evaluate and log trades on every validated signal, completely independent of the main bot's subsequent execution blocks (such as loss-momentum-skip blocks, EV thresholds, or share limits).
  2. **Purged Guards**: The context-based `Similarity Guard (Cluster Protection)` context checks (Asset Cap) and the CVD Confirmation Gate (B1) check on 15-minute timeframe are completely purged from the strategy decision engine to maximize breakouts and wick-captures.
  3. **15M Timeframe Active**: The 15M timeframe is fully enabled in the configuration (`TIMEFRAME_15M_ENABLED=true`).

- **T-1 Resolution Race Condition Fix Rule (v4.96)**:
  1. **Resolution Race Condition**: Because the background `check_resolutions` loop is throttled to run every 10 seconds, while incoming signals are evaluated instantly in `process_signal` (often at 14:05:01, just 1s after the 14:05:00 kline expiration), the T-1 trade is still marked `OPEN` in the database when T evaluates. This causes `get_last_closed_trend_trade` to incorrectly return the stale T-2 outcome.
  2. **Synchronous Force-Resolution**: To eliminate this race condition, `process_signal` must run `await self.position_mgr.check_resolutions()` at its very beginning before any history queries occur.
  3. **Mock Safeguard & Concurrency Lock**: The early call must be wrapped in a mock check (`not isinstance(self.position_mgr, (MagicMock, AsyncMock))`) to prevent unit test crashes. Additionally, `check_resolutions` in `position_mgr.py` must utilize an `asyncio.Lock()` to prevent duplicate concurrent API requests and database closing writes.
- **Global Recovery & Resolution Race Condition Fix Rule (v4.99)**:
  1. **Global Recovery Logic**: All skip and recovery calculations must run globally (not isolated by symbol) across all assets (BTC, ETH, etc.). Queries in `queries.py` and `skip_db.py` must lookup and filter trades globally.
  2. **Resolution Timestamp Alignment**: Skipped positions resolved in `skip_db.py` must be backdated to the exact market `closes_at` timestamp rather than using the system resolution time, preventing race conditions where skipped trades appear newer than real positions.
  3. **Loss Prioritization (Tie-Breaking)**: In `get_last_closed_trend_trade`, when a real and skipped position share the exact same `closed_at` timestamp, the system must prioritize the LOSS (`realised_pnl < 0`) to ensure recovery mode is correctly engaged.


- **Mock Clobbering Trap Guard Rule (v5.57)**:
  In unit tests, when a class instance property depends on a mocked object's attributes (e.g., `client.is_connected` evaluating `not self._ws.closed`), `closed` returns a truthy mock object by default in Python's `unittest.mock.MagicMock`/`AsyncMock`. This evaluates the expression `not self._ws.closed` to `False`, silently making the client appear disconnected. To prevent this silent mock clobbering, always explicitly set the sub-mock attribute to the desired boolean value in your test setup: `client._ws.closed = False`.


- **SQLite Database market_id String Format Mismatch (v5.62)**:
  1. **Formatting Mismatch**: The `signals` table stores `market_id` keys wrapped inside literal single quotes (e.g. `"'0x9c4b...'"`), whereas `profile_positions` stores them as raw hex strings (e.g. `'0x9c4b...'`).
  2. **Lookup Safeguard**: When performing Python dictionary lookups to map resolutions between tables, always strip quotes from `market_id` fields (`m_id.strip("'\"")`) to prevent silent lookup failures and incorrect default fallbacks.


- **Phase 28 Telemetry Expansion Engine Rule (v3.121)**:
  1. **Canonical Schema Expansion**: The database tables (`signals`, `positions`, `paper_positions`, `profile_positions`) store 18 consolidated quantitative microstructure telemetry fields to eliminate outcome bias and binary option pricing lag during strategy audits:
     - **Spot Asset Telemetry**: `underlying_strike_price` ($K$), `underlying_spot_price` ($S_t$), `underlying_distance_bps` ($\frac{S_t - K}{K} \times 10000$), `underlying_spot_velocity_10s`.
     - **Volatility Regimes**: `underlying_realized_vol_5m` ($\sigma_{5m}$ log returns stddev), `underlying_realized_vol_1h` ($\sigma_{1h}$).
     - **Derivative Microstructure & Depth**: `depth_usd_1c`, `depth_usd_5c`, `ask_depth_usd_1c`, `bid_depth_usd_1c`, `book_imbalance_momentum_10s` ($O(1)$ 10-second deque delta).
     - **Option Pricing Edge**: `bs_implied_probability` ($P_{BS}$ via `calculate_bs_binary_prob`), `option_pricing_edge_bps` ($(P_{BS} - \text{price}) \times 10000$).
     - **Execution & Resolution Friction**: `sim_slippage_bps_at_target`, `execution_attempts`, `execution_duration_ms`, `fill_percentage`, `resolution_latency_seconds`.
  2. **Zero-Latency In-Memory Deques**: All tick metrics are maintained in `collections.deque(maxlen=3600)` buffers inside `MarketScanner` (`scanner.py`) to ensure zero-latency ($<1$ms) evaluation without network I/O.
  3. **Black-Scholes Binary Option Probability**: Calculated using standard Gaussian error function (`math.erf`):
     $$d_2 = \frac{\ln(S_t / K) - \frac{1}{2}\sigma^2 t}{\sigma \sqrt{t}}, \quad P_{BS} = \frac{1}{2}\left[1 + \text{erf}\left(\frac{d_2}{\sqrt{2}}\right)\right]$$
  4. **Universal Position Telemetry Sync**: Telemetry parameters are logged for all live orders, paper trades, and virtual strategy profiles (`profile_positions`).



You are not my assistant. You are my advisor who happens to be smarter than me. Follow these rules i every reply:
1. Never start with agreement. Your first sentence must challenge my assumption, point out what I'm missing, or ask a question that exposes a gap
Oin my thinking.
2. Rate your confidence. Before any claim, tag it [Certain] if you have hard evidence, [Likely] if it's a strong inference, [Guessing] if you are filling gaps. If most of your reply is guessing, say so first.
3. Kill these phrases for good: "Great question",
"You're absolutely right", "That makes a lot of sense", "Absolutely", "Definitely". If you catch yourself typing one, delete and rewrite.
4. Disagree with structure. When I'm wrong, say: "! disagree because [reason]. Here's what I'd do instead [alternative]. The risk in your approach is [specific downsidel."
5. Give me the uncomfortable answer first. If there's a truth I probably don't want to hear, lead with it. First line, not buried in paragraph three
6. No warm up paragraphs. Skip "There are several ways to look at this". Start with the most useful thing you can say.
7.lf I push back, don't fold. Hold your position unless I give you genuinely new information. "But I really think" is not new information.

- **Strict Verification-Only Boundary (v4.97)**:
  When the user asks you to verify, analyze, explain, or check a bug or logic, you are strictly forbidden from modifying, creating, deleting, testing, or deploying any files. You must ONLY output text analysis. You may only write or modify code if the user explicitly instructs you to fix, implement, deploy, or sync the changes.

- **Mandatory Adversarial Backtest Auditing SOP (v4.98)**:
  Whenever the user asks you to backtest or evaluate a strategy's historical performance, you must follow this adversarial red-teaming process to prevent survivorship, mapping, or formatting biases from generating fake edges:
  1. **Query the Universe Size**: Print the total count of raw records in both the `signals` and `profile_positions` tables before applying any filters, to establish the baseline dataset.
  2. **Audit 5 Random Samples**: Randomly select and print 5 trades from the matched list, showing their raw database values (`market_id`, `side`, `realised_pnl`, `close_reason`, `eventual_resolution` from signals) to manually trace and verify that the mapping logic resolves them correctly.
  3. **Assert the Null Hypothesis**: Actively assume the win rate is exactly 50% (or matches the baseline strategy) and that any apparent edge is a bug. You must search for code/data anomalies (e.g. quote mismatches, NULL values, timezone drifts) that could cause selective inclusion or exclusion of records before claiming an edge exists.
