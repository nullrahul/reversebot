# Streak Momentum Backtester — Implementation Plan

## Goal

Build a **production-grade backtester** that 100% mimics the live Reversebot trading pipeline, using Binance historical klines as the source of truth. Run two separate modes (2-candle streak vs 1-candle momentum), cache all data locally to avoid API spam, and produce verbose analysis to identify structural edge across different market regimes.

---

## Architecture

```
Binance Klines API  ──▶  Local SQLite Cache  ──▶  Backtest Engine  ──▶  Results Report
  (one-time fetch)       (backtest_cache.db)     (replays bot logic)    (artifact .md)
```

### Why Binance as Source of Truth (Not Gamma/Polymarket)?

The live bot itself falls back to Binance klines when Gamma outcomes are unresolved. Binance klines are:
- Available for any historical date (no Polymarket slug lookups needed)
- Rate-limit-friendly (1200 req/min, we need ~50 total)
- The **actual price oracle** that Polymarket's UP/DOWN markets resolve against

### What We're Simulating

For every 5-minute and 15-minute candle window in the backtest range:
1. **Candle outcome** → `close > open` = UP, else DOWN (exactly like `_get_binance_outcome`)
2. **Streak detection** → Check if the 2 (or 1) preceding candles match (UP-UP or DOWN-DOWN)
3. **Entry price simulation** → Use the live market's YES/NO mid-price from Polymarket price history, or estimate from the Binance candle's position within the range
4. **Streak reset guard** → After a trade, skip the next N candles per `(symbol, window)` key — exactly like `last_traded_close_ts`
5. **Entry filters** → `min_entry_price`, `max_entry_price`, `min_seconds_to_close`
6. **Resolution** → Binary: candle resolves UP → YES=$1/NO=$0, resolves DOWN → YES=$0/NO=$1
7. **P&L** → `shares * exit_price - size_usdc`

---

## Proposed Changes

### [NEW] `R_and_D/backtesting/streak_backtester_v3.py`

Single self-contained Python script (~400 lines). No external dependencies beyond `requests` and `sqlite3` (stdlib).

**Data Layer:**
- `fetch_binance_klines(symbol, interval, start_date, end_date)` → fetches klines, caches to `backtest_cache.db`
- Table: `klines(symbol, interval, open_time, open, high, low, close, volume)` with UNIQUE constraint
- On subsequent runs, reads from cache — zero API calls

**Backtest Engine:**
- `run_backtest(mode, config)` where mode = `"streak2"` or `"streak1"`
- Iterates candles chronologically, applies streak detection, entry price simulation, filters, streak reset guard
- Records each trade in a results list with full metadata

**Entry Price Simulation:**
- The bot enters at the NO (or YES) price during the active candle window. Since we don't have Polymarket orderbook history, we estimate:
  - For a DOWN-DOWN streak buying NO: `no_price ≈ 0.50 ± random noise(0.03)` — the market starts near 50/50 and the streak shifts it
  - Better: use the Binance candle's early movement to estimate where the market would be pricing the continuation. If the current candle opened UP (first few minutes), YES price rises → NO price drops → our NO entry is cheaper. If opened DOWN, NO rises → more expensive.
  - **Conservative approach**: Use a fixed midpoint entry price of $0.48 (the median of our actual live entries from the database). This eliminates price estimation bias and isolates the strategy's directional accuracy.

**Streak Reset Guard (exact live bot logic):**
```python
# After trading candle C at closes_at for (symbol, window):
last_traded_close_ts[(symbol, window)] = closes_at

# For next candidate candle C' at closes_at':
# The 2 lookback candles close at: closes_at' - 2*dur and closes_at' - 3*dur
# Guard: closes_at' - 2*dur must be > last_traded_close_ts[(symbol, window)]
# This means we skip the immediately following candle (its lookback overlaps our trade)
```

**Config (mirrors live bot):**
```python
CONFIG = {
    "symbols": ["BTC", "ETH"],
    "windows": [5, 15],           # minutes
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
    "position_size_usdc": 10.0,
    "simulated_entry_price": 0.48, # conservative midpoint
    "global_cooldown_seconds": 120,
    "min_seconds_to_close": 60,    # 5m markets
    "min_seconds_to_close_15m": 120, # 15m markets
    "streak_reset_enabled": True,
}
```

**Output:**
- Per-trade verbose log (date, time ET, symbol, window, streak, side, entry_price, outcome, pnl)
- Summary tables: overall, per-asset×window, per-day-of-week, per-hour-ET, entry-price-bucket
- Weekday vs Weekend split
- Streak-2 vs Streak-1 comparison table

### [NEW] `R_and_D/backtesting/backtest_cache.db`

Auto-created SQLite database. Stores:
- `klines` table: Binance kline data (symbol, interval, timestamps, OHLCV)
- Reused across all future backtest runs

---

## Backtest Range

- **Start**: May 1, 2026 (gives us ~23 days of data including weekdays + weekends)
- **End**: May 23, 2026 (today)
- This covers multiple Saturday/Sunday cycles for weekend vs weekday analysis

---

## Two Modes (Separate Results, No Overlap)

| Mode | Signal Condition | Streak Reset | Description |
|------|-----------------|--------------|-------------|
| `streak2` | 2 consecutive same-direction candles (UP-UP or DOWN-DOWN) | YES — skips next candle after trade | **Current live bot logic** |
| `streak1` | 1 candle direction confirmed | YES — skips next candle after trade | Single-candle momentum (more signals, less confirmation) |

Results stored in separate sections of the output report. Different trade sets, different P&L.

---

## Verification Plan

### Automated
1. Run the backtester locally: `python3 R_and_D/backtesting/streak_backtester_v3.py`
2. Cross-reference streak2 results against live paper trading data (our 95 filled trades) for the overlapping date range
3. Verify cache database was created and populated (no repeat API calls on re-run)

### Manual
- Compare backtester win rates per segment against the live trade analysis in [trade_analysis_may23.md](file:///Users/rahulkumar/.gemini/antigravity/brain/5693df13-a792-4baf-a159-a88154e89040/trade_analysis_may23.md)
- Validate that weekend days show different patterns vs weekdays

---

## Open Questions

> [!IMPORTANT]
> **Entry Price Estimation**: The biggest source of backtest bias. Our live entries range $0.42-$0.70. Using a fixed $0.48 midpoint is conservative but loses the price-band edge analysis. Should I:
> - **(A)** Use fixed $0.48 for all trades (cleanest — isolates directional accuracy)
> - **(B)** Randomize entry price uniformly between $0.45-$0.55 (adds noise but tests price band effect)
> - **(C)** Use Binance candle structure to estimate (if early candle confirms streak direction, entry is cheaper; if contradicts, entry is more expensive)

> [!NOTE]
> **Backtest range**: Starting May 1 gives ~23 days. Should I go further back (April?) for more data, or is 23 days sufficient for initial analysis?
