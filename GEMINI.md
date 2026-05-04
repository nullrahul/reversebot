# ReverseBot Project Logs & Status

## Project Goal
Stabilize RuFlo V3 HFT Infrastructure for reliable crypto scalping on Polymarket.

## Environment
- **Local**: MacBook Air M1 (8GB)
- **VPS**: Ubuntu @ 68.210.96.175 (PM2: `reversebot_rd`)
- **Mode**: R&D (Paper Trade on `crypto_short_window`)

## Recent Progress (2026-04-24)

### 🛠️ Key Fixes & Improvements (Deployed 2026-04-24)
1. **Global Cooldown (Strict Limit)**:
   - **Problem**: 4 positions opened in 2 seconds despite a 60s cooldown setting.
   - **Cause**: `allow_bypass` flag was overriding the global cooldown during "Double-Dip Alignment".
   - **Fix**: Removed the bypass exception from the `global_cooldown` check in `src/strategy/decision.py`. It is now a **Hard Limit**.
   - **Config**: Manually corrected `global_cooldown` to `60.0` in `dashboard/runtime_config_rd.json`.

2. **Deployment & Process Management**:
   - **Action**: Successfully synchronized local changes to VPS (`68.210.96.175`) via `rsync`.
   - **Action**: Restarted `reversebot_rd` PM2 process to apply new logic and config.

3. **Manual Position Closure**:
   - **Fix**: Implemented `manual_close_position` with aggressive FOK sell logic.

4. **Database Integrity**:
   - **Fix**: Added explicit `upsert_market` call in the scanner's discovery loop.

### 🛠️ Key Fixes & Improvements (Deployed 2026-04-25)
1. **Split Take Profit (HFT Scaling)**:
   - **Feature**: Positions now split into two exit targets (TP1 and TP2).
   - **Logic**: Sells a fixed amount (default 8 shares) at 1.5x multiplier, and the remainder at 2.5x multiplier.
   - **Fix (PnL Accuracy)**: Refactored `PositionManager` to track `_partial_proceeds`. Previously, the final closure would 'forget' TP1 profits and report a total loss if the remainder hit SL.

2. **Volatility & Spike Protection**:
   - **Spread Guard**: Re-enabled `check_2` in `validator.py`. The bot now correctly rejects markets with wide bid-ask spreads (e.g. 50% spreads) which were causing instant Stop Loss triggers.
   - **SL Confirmation**: Increased spike protection from 400ms to 2.5s.
   - **Price Sourcing**: Monitor now falls back to `price_cache` (mid) if `bid_cache` (sell) is temporarily empty, preventing phantom liquidations.

3. **Resolution Resolver (Stuck Trades Fix)**:
   - **Fix**: Implemented `check_resolutions` to poll Gamma API for outcomes of expired markets.
   - **Result**: Bot now automatically settles positions when markets resolve.

### 🛠️ Key Fixes & Improvements (Deployed 2026-04-25 - Part 2)
1. **Persistence Logic (JSON Notes Fix)**:
   - **Problem**: The bot "forgot" its profits after a restart because the `notes` field in the DB was not being parsed correctly as JSON.
   - **Fix**: Implemented `_get_tid_from_notes` in `position_mgr.py` to safely extract `token_id` regardless of whether `notes` is a raw string or a JSON object (`{"tid": "token_id", "p": proceeds, "r": remaining}`). This ensures tracking data is reloaded on restart.

2. **Paper-Mode TP Fix (Instant Fill Bug)**:
   - **Problem**: Every trade was going until resolution because Paper TP orders were instantly considered "MATCHED" by the clob client, causing them to close incorrectly or be ignored.
   - **Fix**: Updated `monitor_positions` to explicitly simulate Paper TP fills ONLY when `current_no_price >= target_price`. 

3. **Price Sourcing (Anti-Blindness Fix)**:
   - **Problem**: If the NO token had 0 liquidity (empty bid), the bot went blind and missed Stop Loss triggers.
   - **Fix**: The bot now calculates an Implied Price using `1.0 - YES_ASK` when `current_no_price` is missing, preventing phantom liquidations or missed stops.

4. **API Limits (Split-TP shares)**:
   - **Verification**: The split-TP logic correctly ensures that both TP halves are >= 5 shares (Polymarket's minimum), otherwise it defaults to a single TP order.

5. **P&L Check (Dashboard Accuracy)**:
   - **Fix**: Updated `dashboard.py` to parse the JSON from `notes` and use the implied `1.0 - YES_ASK` bid to correctly calculate `live_pnl` as `(CurrentValue * 0.98) - CostBasis`.

### 🛠️ Key Fixes & Improvements (Deployed 2026-04-25 - Part 3)
1. **Stop Loss Accuracy (The "Positive Loss" Bug)**:
   - **Problem**: The Stop Loss calculation ignored partial proceeds from TP1. If TP1 secured profit but the remaining shares triggered the Stop Loss, the bot closed the position as "STOP_LOSS" but with a net positive PnL, causing the dashboard to count it as a "win." The SL threshold was comparing the reduced `current_value` of the remaining shares against the *total* initial cost basis, causing premature triggering.
   - **Fix**: Updated `monitor_positions` to calculate `total_current_value` by adding `_partial_proceeds` to the remaining `current_value`. The `sl_threshold` now accurately compares against the aggregate net value of the entire position, preventing premature triggers and "profitable stop losses."

2. **Dashboard Live P&L Accuracy (Partial Fills)**:
   - **Problem**: The dashboard `live_pnl` formula was using the original `shares_bought`, completely ignoring that some shares were already sold via TP1. This resulted in heavily negative or wildly inaccurate live P&Ls for positions that were actually in profit.
   - **Fix**: Updated `dashboard.py` to parse `notes` for `r` (remaining shares) and `p` (partial proceeds), adjusting the `current_value` calculation to use `(live_no * shares_remaining * 0.98) + partial_proceeds - size_usdc`.

### 📋 Performance & Stability Notes
- **Status**: **V14.3 (SL & Dashboard P&L Fix)** is ONLINE.
- **Monitoring**: Watching for `resting_tp_partial_filled` logs to verify scaling execution and proceeds tracking.
- **Weakness**: Very illiquid markets might still be tricky; spread guard is the primary defense.

## Current State
- **Bot Version**: V14.2 (Anti-Blindness & JSON Persistence)
- **Status**: **ONLINE** (PM2: `reversebot_rd`)
- **Next Step**: Monitor trades to confirm TP/SL triggers correctly and don't drift to MARKET_OUTCOME.
### 🛠️ Key Fixes & Improvements (Deployed - Deep Clean)
1. **Hallucination Loop (Honest Paper Engine)**:
   - **Fix**: Re-wrote `get_order_status` and `wait_for_fill` in `clob_client.py` to completely eliminate "instant match" assumptions. Paper TP orders now remain `PENDING` until the actual WebSocket price crosses the limit target.
2. **Stateful Position Manager**:
   - **Fix**: Fortified the `_partial_proceeds` tracking. Any partial fill is mathematically locked via the JSON `notes` DB field to perfectly recover across PM2 restarts.
3. **Aggressive Exit Logic**:
   - **Fix**: Added "3-Strike FOK Sell" to Stop Losses in `position_mgr.py`. It attempts exits at 1%, 3%, and 5% progressive slippage rather than giving up if the order book moves too fast.
4. **PnL Formula Rot**:
   - **Fix**: Audited and fixed `math_utils.py` to cleanly account for `partial_proceeds` in `realised_pnl` and `unrealised_pnl`. Verified `dashboard.py` `live_pnl` math.
5. **UI Deception (Loss Hiding)**:
   - **Fix**: Cleaned up the exit `reason` logic so that `STOP_LOSS` is correctly reported for pure losses, and `PARTIAL_TP` is recorded if stopped out after a partial TP1 win, preventing loss camouflage.

### 🛠️ Key Fixes & Improvements (Deployed - Trade Audit Fix)
1. **The Hallucination Loop**:
   - **Fix**: The previous attempt missed a crucial bypass in `position_mgr.py` where paper trades were still auto-filling without checking `get_order_status`. I completely removed the `if self._paper` bypass in `monitor_positions` and `_create_resting_tp_orders`. Now, all Paper orders (including TPs) generate real `orderID`s and strictly poll `clob_client.py` for status, exactly like live orders.
