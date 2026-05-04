# R&D (Research & Development)

This folder is dedicated to testing new strategies, API queries, and optimizations before integrating them into the main bot.

## Current Experiments:

### 1. `crypto_short_window_tests/`
**Goal:** Reliably identify and subscribe to hyper-short (5-minute and 15-minute) Crypto price prediction markets ("Up or Down" markets).
*   **Why:** Crypto markets are efficient, but 5m/15m windows offer high volatility and frequent expiration cycles. This allows for rapid capital turnover.
*   **Scripts:**
    *   `fetch_short_markets.py`: A standalone script to query the Gamma API specifically looking for 5m, 15m, and short timestamp-bounded markets (e.g., "Bitcoin Up or Down 8:25AM-8:30AM").

### Future Ideas to Explore Here:
*   **CVD Guard Tuning:** Scripts to fetch historical order book snapshots and test the "Yes vs No buying volume" ratio.
*   **Maker Mode (0% Fee) Simulation:** Scripts to test placing resting limit orders slightly below the current ask price to avoid Taker fees.
*   **Theta Decay Logic:** Simulating the PnL difference if positions were automatically dumped halfway through their lifetime if not in profit.
