# Reversal & Trend-Following Backtesting Report

This report compares standard **Trend-Following (Baseline)** against **Option A (Regime-based toggling)** and **Option B (Performance-based toggling)** across two distinct periods.

---

## 📊 Run 1: Full Historical Range
*Date Range: 2026-03-31 20:00 to 2026-06-05 13:58 UTC*
*Position Size: $10.00 USDC flat. Concurrency Limit: 2. Max slots per asset: 1. Strict Streak Reset Guard enforced.*

| Strategy Configuration | Total Trades | TF Trades | Rev Trades | Win Rate | Net PnL (USDC) | ROI | Max DD (%) | Profit Factor | Streak Reset Blocks |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Baseline TF | 10981 | 10981 | 0 | 48.45% | +140.44 | +0.1% | 77.65% | 1.0 | 5317 |
| Option A (T1: P>=0.55 / P<=0.45) | 7704 | 4597 | 3107 | 48.34% | -3092.35 | -4.0% | 311.65% | 0.92 | 3176 |
| Option A (T2: P>=0.52 / P<=0.48) | 10058 | 5751 | 4307 | 48.59% | -3797.87 | -3.8% | 373.26% | 0.93 | 4374 |
| Option A (T3: P>=0.50 / P<0.50) | 10981 | 6674 | 4307 | 48.95% | -2949.43 | -2.7% | 311.55% | 0.95 | 5317 |
| Option B (B1: N=10, WR<45%) | 10981 | 5055 | 5926 | 53.02% | +4292.16 | +3.9% | 35.04% | 1.08 | 5317 |
| Option B (B2: N=20, WR<45%) | 10981 | 4785 | 6196 | 52.64% | +3210.34 | +2.9% | 27.16% | 1.06 | 5317 |
| Option B (B3: N=10, WR<40%) | 10981 | 5028 | 5953 | 52.24% | +2604.07 | +2.4% | 37.83% | 1.05 | 5317 |
| Option B (B4: N=20, WR<40%) | 10981 | 4522 | 6459 | 51.73% | +986.95 | +0.9% | 64.59% | 1.02 | 5317 |

---

## 📊 Run 2: Recent Period (June 3 - June 5, 2026)
*Date Range: 2026-06-03 00:00 to 2026-06-05 13:58 UTC (High Volatility & Choppiness)*
*Position Size: $10.00 USDC flat. Concurrency Limit: 2. Max slots per asset: 1. Strict Streak Reset Guard enforced.*

| Strategy Configuration | Total Trades | TF Trades | Rev Trades | Win Rate | Net PnL (USDC) | ROI | Max DD (%) | Profit Factor | Streak Reset Blocks |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Baseline TF | 513 | 513 | 0 | 45.61% | -281.12 | -5.5% | 56.22% | 0.9 | 232 |
| Option A (T1: P>=0.55 / P<=0.45) | 362 | 249 | 113 | 48.07% | -140.64 | -3.9% | 26.19% | 0.93 | 136 |
| Option A (T2: P>=0.52 / P<=0.48) | 465 | 302 | 163 | 49.68% | -39.52 | -0.8% | 23.57% | 0.98 | 187 |
| Option A (T3: P>=0.50 / P<0.50) | 513 | 350 | 163 | 49.71% | -23.12 | -0.5% | 25.6% | 0.99 | 232 |
| Option B (B1: N=10, WR<45%) | 513 | 202 | 311 | 55.17% | +385.93 | +7.5% | 10.77% | 1.17 | 232 |
| Option B (B2: N=20, WR<45%) | 513 | 195 | 318 | 54.58% | +317.76 | +6.2% | 11.22% | 1.14 | 232 |
| Option B (B3: N=10, WR<40%) | 513 | 224 | 289 | 54.0% | +288.20 | +5.6% | 10.67% | 1.12 | 232 |
| Option B (B4: N=20, WR<40%) | 513 | 209 | 304 | 48.73% | -261.35 | -5.1% | 33.23% | 0.9 | 232 |

---

## 🛠️ Data Integrity & Methodology

### 1. Data Source
*   **Source**: Actual Binance 1-minute historical kline data cached locally in [backtest_cache.db](file:///Users/rahulkumar/Documents/reversebot/R_and_D/backtesting/backtest_cache.db).
*   **Total rows analyzed**: 168232 1-minute records (BTC: 84116, ETH: 84116).
*   **Verification**: Covers March 31, 2026 to June 5, 2026, including the recent high-volatility session of June 3-5.

### 2. The Streak Reset Guard Logic
*   The simulator uses the exact formula implemented in the live bot's [scanner.py](file:///Users/rahulkumar/Documents/reversebot/src/strategy/scanner.py):
    ```python
    m_prev2_close = closes_at - (2 * dur_secs)
    if m_prev2_close < last_traded:
        continue
    ```
*   This ensures that the two completed candles forming the streak (Candle 1 and Candle 2) close strictly after our last executed trade on that asset, preventing overlapping trades on the same market wiggle.
*   The **Streak Reset Blocks** column shows how many times this guard successfully filtered out overlapping/redundant signals.

### 3. Price & Fee Simulation
*   **Entry Price (Option C replication)**: Estimated based on the first-minute return of the trade candle:
    *   *Trend-Following*: 0.48 - (5.0 * first_minute_return) (clamped between 0.30 and 0.60 to match B3 price boundaries).
    *   *Reversal/Fade*: 1.0 - trend_price + 0.01 (includes a 1 cent spread/slippage penalty for opposing entries).
*   **Fees**: Replicates Polymarket's 2% fee on net winnings on all winning trades.

## 💻 How to Reproduce and Verify These Results
You can run this backtest on your MacBook Air M1 to audit the code and confirm there are no hardcoded/mocked outputs:
```bash
# 1. Activate the local virtual environment
source venv_local/bin/activate

# 2. Run the backtester script
python3 R_and_D/backtesting/reversal_backtester.py
```

