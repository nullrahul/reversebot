# May 23, 2026 Mimic Verification & Backtest Report

Generated at: 2026-05-24 07:55:48 UTC
Target Audit Date: **2026-05-23**

## 📊 Executive Summary

This report performs a trade-by-trade audit comparing the **V3 Backtester** (simulated from 1m Binance klines using Option C entry pricing and integer share rounding) against the **Actual Paper Trades** executed by the live Reversebot on the VPS.

| Metric | Backtester (Simulated) | Live Bot (Actual Paper) | Discrepancy |
| --- | --- | --- | --- |
| **Total Trades** | 184 | 122 | -62 trades |
| **Wins** | 83 | 69 | -14 wins |
| **Win Rate** | 45.11% | 56.56% | +11.45% |
| **Total P&L** | -104.55 USDC | +156.82 USDC | +261.36 USDC |

---

## 🔍 Mimic & Fidelity Audit

- **Total Matched Trades**: 86
- **Fidelity Match Rate (Backtest)**: 46.7% (Matched 86 / 184 simulated trades)
- **Fidelity Match Rate (Bot)**: 70.5% (Matched 86 / 122 actual trades)
- **Win/Loss Resolution Mismatches**: 3 (Trades where backtester and bot had different outcomes)
- **Average Entry Price Discrepancy (Simulated - Actual)**: -0.0184
- **Mean Absolute Error (MAE) on Entry Price**: 0.0391 USDC
- **Mean Absolute Error (MAE) on Trade P&L**: 1.62 USDC

### ⚖️ Verdict: Does the Backtester Mimic the Bot?

> [!NOTE]
> **Fidelity Verdict**: **MODERATE FIDELITY (50% - 79% Match)**
> 
> **Fidelity Match Rate (Bot)** is **70.49%**.
> **Option C Price Simulation MAE**: **0.0391**. This demonstrates that the Option C price simulation (first 1-minute Binance candle return proxy) is a moderate proxy for real order-book entry prices on Polymarket.

---

## 🔬 Discrepancy Details

### ⚠️ Bot Trades NOT Found in Backtest (Missing in Backtest)
*Count: 36*

The following actual trades were taken by the bot but NOT simulated in the backtest. This typically occurs due to:
1. **Spread / Slippage Filter Difference**: The backtester assumes a single price, whereas the bot rejects trades if the order book spread is too wide.
2. **Timing & Scanner Lag**: Minor differences in when kline data becomes available vs when the bot pulls Gamma API.

| Time (UTC) | Asset | Window | Side | Entry (Actual) | PnL (Actual) |
| --- | --- | --- | --- | --- | --- |
| 2026-05-23 01:05:06 UTC | ETH | 5m | YES | $0.44 | $+12.32 |
| 2026-05-23 01:30:01 UTC | BTC | 15m | YES | $0.49 | $-9.80 |
| 2026-05-23 03:35:07 UTC | ETH | 5m | YES | $0.48 | $+10.40 |
| 2026-05-23 03:50:01 UTC | ETH | 5m | NO | $0.53 | $-9.54 |
| 2026-05-23 05:55:06 UTC | BTC | 5m | NO | $0.47 | $-9.87 |
| 2026-05-23 06:20:50 UTC | ETH | 15m | NO | $0.47 | $+11.13 |
| 2026-05-23 07:01:54 UTC | BTC | 15m | NO | $0.45 | $+12.10 |
| 2026-05-23 07:50:06 UTC | ETH | 5m | NO | $0.56 | $+7.48 |
| 2026-05-23 08:02:41 UTC | ETH | 15m | NO | $0.57 | $-9.69 |
| 2026-05-23 08:20:07 UTC | ETH | 5m | YES | $0.49 | $+10.20 |
| 2026-05-23 10:04:30 UTC | ETH | 15m | NO | $0.45 | $-9.90 |
| 2026-05-23 10:35:00 UTC | ETH | 5m | YES | $0.48 | $+10.40 |
| 2026-05-23 10:52:28 UTC | BTC | 5m | NO | $0.45 | $+12.10 |
| 2026-05-23 11:05:05 UTC | BTC | 5m | YES | $0.50 | $+10.00 |
| 2026-05-23 11:31:32 UTC | ETH | 15m | YES | $0.46 | $-9.66 |
| 2026-05-23 12:47:07 UTC | ETH | 15m | YES | $0.47 | $+11.13 |
| 2026-05-23 13:33:53 UTC | BTC | 15m | YES | $0.45 | $-9.90 |
| 2026-05-23 14:30:15 UTC | ETH | 15m | YES | $0.50 | $+10.00 |
| 2026-05-23 14:51:21 UTC | ETH | 5m | YES | $0.71 | $+4.06 |
| 2026-05-23 14:53:18 UTC | BTC | 15m | YES | $0.45 | $+12.10 |
| 2026-05-23 15:05:03 UTC | ETH | 5m | YES | $0.47 | $+11.13 |
| 2026-05-23 15:18:37 UTC | BTC | 5m | YES | $0.45 | $+12.10 |
| 2026-05-23 15:25:03 UTC | ETH | 5m | NO | $0.54 | $+8.28 |
| 2026-05-23 18:36:38 UTC | BTC | 15m | YES | $0.64 | $+5.40 |
| 2026-05-23 20:18:27 UTC | ETH | 15m | YES | $0.58 | $-9.86 |
| 2026-05-23 20:40:01 UTC | ETH | 5m | YES | $0.59 | $+6.56 |
| 2026-05-23 20:55:02 UTC | ETH | 5m | YES | $0.46 | $+11.34 |
| 2026-05-23 21:25:08 UTC | ETH | 5m | NO | $0.65 | $+5.25 |
| 2026-05-23 21:33:14 UTC | BTC | 15m | NO | $0.65 | $+5.25 |
| 2026-05-23 21:39:39 UTC | ETH | 15m | NO | $0.45 | $+12.10 |
| 2026-05-23 21:50:08 UTC | ETH | 5m | NO | $0.31 | $-9.92 |
| 2026-05-23 22:05:04 UTC | ETH | 5m | YES | $0.45 | $-9.90 |
| 2026-05-23 23:03:53 UTC | ETH | 15m | YES | $0.45 | $+12.10 |
| 2026-05-23 23:20:05 UTC | ETH | 5m | YES | $0.46 | $-9.66 |
| 2026-05-23 23:20:11 UTC | BTC | 15m | YES | $0.65 | $-9.75 |
| 2026-05-23 23:36:53 UTC | BTC | 5m | YES | $0.45 | $-9.90 |

### ⚠️ Backtest Trades NOT Found in Bot (Extra in Backtest)
*Count: 98*

The following trades were simulated in the backtester but NOT taken by the live bot. This typically occurs because:
1. **No Market / Zero Volume**: The backtest checks all historical klines, but on Polymarket, a specific 5m or 15m window market might not have been created or active at that time.
2. **Concurrency / Exposure Limits**: The bot might have hit its `max_open_positions` limit (50) or capital limits.

| Time (UTC) | Asset | Window | Side | Entry (Simulated) | PnL (Simulated) |
| --- | --- | --- | --- | --- | --- |
| 2026-05-23 00:10:00 UTC | ETH | 5m | YES | $0.48 | $-9.69 |
| 2026-05-23 00:15:00 UTC | BTC | 15m | NO | $0.48 | $-9.58 |
| 2026-05-23 00:30:00 UTC | BTC | 5m | YES | $0.48 | $-9.67 |
| 2026-05-23 00:30:00 UTC | ETH | 15m | YES | $0.49 | $-9.72 |
| 2026-05-23 00:45:00 UTC | BTC | 5m | NO | $0.48 | $-9.65 |
| 2026-05-23 00:45:00 UTC | ETH | 5m | NO | $0.48 | $-9.68 |
| 2026-05-23 01:00:00 UTC | ETH | 5m | YES | $0.47 | $+11.04 |
| 2026-05-23 01:10:00 UTC | BTC | 5m | YES | $0.48 | $-9.62 |
| 2026-05-23 01:15:00 UTC | BTC | 15m | YES | $0.48 | $+10.40 |
| 2026-05-23 01:50:00 UTC | BTC | 5m | NO | $0.48 | $-9.61 |
| 2026-05-23 01:50:00 UTC | ETH | 5m | NO | $0.48 | $-9.62 |
| 2026-05-23 02:05:00 UTC | ETH | 5m | NO | $0.48 | $-9.66 |
| 2026-05-23 02:20:00 UTC | ETH | 5m | NO | $0.48 | $-9.58 |
| 2026-05-23 02:30:00 UTC | BTC | 5m | NO | $0.48 | $-9.59 |
| 2026-05-23 02:30:00 UTC | ETH | 15m | NO | $0.48 | $-9.58 |
| 2026-05-23 02:35:00 UTC | ETH | 5m | NO | $0.48 | $+10.43 |
| 2026-05-23 02:45:00 UTC | BTC | 15m | YES | $0.48 | $+10.40 |
| 2026-05-23 02:50:00 UTC | ETH | 5m | YES | $0.48 | $-9.62 |
| 2026-05-23 03:00:00 UTC | BTC | 5m | YES | $0.48 | $+10.41 |
| 2026-05-23 03:15:00 UTC | ETH | 15m | YES | $0.48 | $+10.36 |
| 2026-05-23 03:30:00 UTC | ETH | 5m | YES | $0.48 | $+10.43 |
| 2026-05-23 03:40:00 UTC | BTC | 5m | YES | $0.48 | $+10.40 |
| 2026-05-23 03:45:00 UTC | ETH | 5m | YES | $0.48 | $-9.62 |
| 2026-05-23 04:45:00 UTC | BTC | 5m | NO | $0.48 | $-9.60 |
| 2026-05-23 04:45:00 UTC | BTC | 15m | NO | $0.48 | $-9.60 |
| 2026-05-23 04:45:00 UTC | ETH | 5m | NO | $0.48 | $-9.63 |
| 2026-05-23 04:45:00 UTC | ETH | 15m | NO | $0.48 | $-9.63 |
| 2026-05-23 05:30:00 UTC | ETH | 15m | NO | $0.48 | $-9.61 |
| 2026-05-23 05:35:00 UTC | ETH | 5m | YES | $0.48 | $+10.40 |
| 2026-05-23 05:45:00 UTC | BTC | 5m | YES | $0.48 | $-9.59 |
| 2026-05-23 05:55:00 UTC | ETH | 5m | NO | $0.48 | $-9.61 |
| 2026-05-23 06:15:00 UTC | ETH | 15m | NO | $0.48 | $+10.39 |
| 2026-05-23 06:30:00 UTC | BTC | 5m | NO | $0.48 | $-9.60 |
| 2026-05-23 06:30:00 UTC | ETH | 5m | NO | $0.48 | $-9.60 |
| 2026-05-23 07:00:00 UTC | BTC | 15m | NO | $0.48 | $+10.34 |
| 2026-05-23 07:05:00 UTC | BTC | 5m | NO | $0.48 | $+10.45 |
| 2026-05-23 07:35:00 UTC | ETH | 5m | NO | $0.48 | $-9.62 |
| 2026-05-23 07:55:00 UTC | BTC | 5m | NO | $0.48 | $-9.58 |
| 2026-05-23 07:55:00 UTC | ETH | 5m | NO | $0.48 | $-9.60 |
| 2026-05-23 08:00:00 UTC | ETH | 15m | NO | $0.48 | $-9.53 |
| 2026-05-23 08:15:00 UTC | ETH | 5m | YES | $0.48 | $+10.44 |
| 2026-05-23 08:45:00 UTC | BTC | 15m | NO | $0.48 | $-9.61 |
| 2026-05-23 08:50:00 UTC | ETH | 5m | YES | $0.48 | $+10.43 |
| 2026-05-23 09:50:00 UTC | BTC | 5m | YES | $0.48 | $-9.59 |
| 2026-05-23 10:00:00 UTC | ETH | 15m | NO | $0.48 | $-9.63 |
| 2026-05-23 10:10:00 UTC | ETH | 5m | YES | $0.48 | $+10.47 |
| 2026-05-23 10:25:00 UTC | ETH | 5m | NO | $0.48 | $-9.65 |
| 2026-05-23 10:35:00 UTC | BTC | 5m | YES | $0.48 | $+10.38 |
| 2026-05-23 10:40:00 UTC | ETH | 5m | YES | $0.48 | $+10.39 |
| 2026-05-23 10:50:00 UTC | BTC | 5m | NO | $0.48 | $+10.40 |
| 2026-05-23 11:10:00 UTC | BTC | 5m | YES | $0.48 | $-9.58 |
| 2026-05-23 11:30:00 UTC | BTC | 15m | YES | $0.48 | $-9.62 |
| 2026-05-23 11:30:00 UTC | ETH | 15m | YES | $0.48 | $-9.63 |
| 2026-05-23 11:40:00 UTC | ETH | 5m | NO | $0.48 | $-9.62 |
| 2026-05-23 12:45:00 UTC | ETH | 15m | YES | $0.48 | $+10.38 |
| 2026-05-23 12:50:00 UTC | BTC | 5m | NO | $0.48 | $+10.41 |
| 2026-05-23 13:30:00 UTC | BTC | 15m | YES | $0.48 | $-9.53 |
| 2026-05-23 13:30:00 UTC | ETH | 15m | YES | $0.48 | $-9.55 |
| 2026-05-23 13:40:00 UTC | BTC | 5m | NO | $0.48 | $+10.45 |
| 2026-05-23 14:15:00 UTC | ETH | 15m | YES | $0.48 | $+10.38 |
| 2026-05-23 14:30:00 UTC | ETH | 5m | YES | $0.48 | $+10.38 |
| 2026-05-23 14:45:00 UTC | BTC | 15m | YES | $0.48 | $+10.44 |
| 2026-05-23 14:45:00 UTC | ETH | 5m | YES | $0.47 | $+11.15 |
| 2026-05-23 15:00:00 UTC | BTC | 5m | YES | $0.48 | $-9.64 |
| 2026-05-23 15:00:00 UTC | ETH | 5m | YES | $0.48 | $+10.37 |
| 2026-05-23 15:00:00 UTC | ETH | 15m | YES | $0.48 | $+10.37 |
| 2026-05-23 15:15:00 UTC | BTC | 5m | YES | $0.48 | $+10.33 |
| 2026-05-23 15:15:00 UTC | ETH | 5m | YES | $0.49 | $-9.71 |
| 2026-05-23 15:30:00 UTC | ETH | 5m | NO | $0.48 | $+10.47 |
| 2026-05-23 15:45:00 UTC | ETH | 15m | NO | $0.48 | $-9.68 |
| 2026-05-23 16:00:00 UTC | BTC | 5m | YES | $0.47 | $+11.04 |
| 2026-05-23 16:35:00 UTC | ETH | 5m | YES | $0.48 | $+10.45 |
| 2026-05-23 17:35:00 UTC | ETH | 5m | YES | $0.48 | $-9.64 |
| 2026-05-23 18:15:00 UTC | BTC | 15m | YES | $0.48 | $+10.40 |
| 2026-05-23 18:20:00 UTC | ETH | 5m | YES | $0.47 | $-9.82 |
| 2026-05-23 19:15:00 UTC | BTC | 15m | NO | $0.48 | $-9.69 |
| 2026-05-23 19:15:00 UTC | ETH | 15m | NO | $0.49 | $-9.71 |
| 2026-05-23 20:15:00 UTC | BTC | 15m | YES | $0.48 | $-9.59 |
| 2026-05-23 20:15:00 UTC | ETH | 15m | YES | $0.48 | $-9.62 |
| 2026-05-23 20:30:00 UTC | ETH | 5m | NO | $0.49 | $-9.81 |
| 2026-05-23 20:45:00 UTC | ETH | 5m | YES | $0.47 | $+11.07 |
| 2026-05-23 21:00:00 UTC | BTC | 15m | YES | $0.48 | $-9.53 |
| 2026-05-23 21:00:00 UTC | ETH | 5m | YES | $0.48 | $-9.66 |
| 2026-05-23 21:00:00 UTC | ETH | 15m | YES | $0.48 | $-9.66 |
| 2026-05-23 21:20:00 UTC | ETH | 5m | NO | $0.48 | $+10.33 |
| 2026-05-23 21:45:00 UTC | BTC | 5m | NO | $0.48 | $+10.36 |
| 2026-05-23 21:45:00 UTC | BTC | 15m | NO | $0.48 | $-9.64 |
| 2026-05-23 21:45:00 UTC | ETH | 5m | NO | $0.48 | $+10.36 |
| 2026-05-23 21:45:00 UTC | ETH | 15m | NO | $0.48 | $-9.64 |
| 2026-05-23 22:00:00 UTC | ETH | 5m | YES | $0.48 | $+10.47 |
| 2026-05-23 22:15:00 UTC | ETH | 5m | NO | $0.48 | $-9.59 |
| 2026-05-23 22:30:00 UTC | BTC | 5m | NO | $0.48 | $-9.54 |
| 2026-05-23 22:30:00 UTC | ETH | 15m | NO | $0.48 | $-9.53 |
| 2026-05-23 23:15:00 UTC | BTC | 15m | YES | $0.48 | $-9.54 |
| 2026-05-23 23:15:00 UTC | ETH | 5m | YES | $0.48 | $+10.45 |
| 2026-05-23 23:15:00 UTC | ETH | 15m | YES | $0.48 | $-9.55 |
| 2026-05-23 23:30:00 UTC | ETH | 5m | NO | $0.48 | $-9.66 |
| 2026-05-23 23:35:00 UTC | BTC | 5m | YES | $0.48 | $-9.64 |

---

## 📈 Detailed Matched Trade Sample

Below is a comparison of matched trades (first 25 shown):

| Time (UTC) | Asset | Window | Side | Entry (Sim) | Entry (Actual) | Price Diff | PnL (Sim) | PnL (Actual) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-23 00:10:00 UTC | BTC | 5m | YES | $0.48 | $0.50 | -0.02 | $-9.67 | $-10.00 |
| 2026-05-23 00:30:00 UTC | ETH | 5m | YES | $0.49 | $0.45 | +0.04 | $-9.72 | $-9.90 |
| 2026-05-23 01:25:00 UTC | ETH | 5m | YES | $0.48 | $0.53 | -0.05 | $+10.41 | $+8.46 |
| 2026-05-23 02:45:00 UTC | BTC | 5m | YES | $0.48 | $0.51 | -0.03 | $+10.40 | $+9.31 |
| 2026-05-23 04:05:00 UTC | ETH | 5m | NO | $0.48 | $0.50 | -0.02 | $+10.43 | $+10.00 |
| 2026-05-23 04:25:00 UTC | ETH | 5m | NO | $0.48 | $0.46 | +0.02 | $+10.40 | $+11.34 |
| 2026-05-23 05:05:00 UTC | BTC | 5m | YES | $0.48 | $0.45 | +0.03 | $+10.41 | $+12.10 |
| 2026-05-23 05:05:00 UTC | ETH | 5m | YES | $0.48 | $0.54 | -0.06 | $-9.57 | $-9.72 |
| 2026-05-23 05:30:00 UTC | BTC | 15m | NO | $0.48 | $0.50 | -0.02 | $-9.61 | $-10.00 |
| 2026-05-23 06:15:00 UTC | BTC | 15m | NO | $0.48 | $0.48 | +0.00 | $+10.39 | $+10.72 |
| 2026-05-23 07:05:00 UTC | ETH | 5m | NO | $0.48 | $0.45 | +0.03 | $+10.43 | $+12.10 |
| 2026-05-23 07:15:00 UTC | ETH | 15m | NO | $0.48 | $0.62 | -0.14 | $+10.46 | $+6.08 |
| 2026-05-23 07:25:00 UTC | BTC | 5m | NO | $0.48 | $0.45 | +0.03 | $-9.60 | $-9.90 |
| 2026-05-23 07:45:00 UTC | BTC | 15m | NO | $0.48 | $0.70 | -0.22 | $+10.44 | $+4.20 |
| 2026-05-23 08:10:00 UTC | BTC | 5m | NO | $0.49 | $0.46 | +0.03 | $-9.76 | $-9.66 |
| 2026-05-23 08:25:00 UTC | BTC | 5m | YES | $0.48 | $0.53 | -0.05 | $-9.63 | $-9.54 |
| 2026-05-23 08:35:00 UTC | ETH | 5m | NO | $0.48 | $0.48 | -0.00 | $+10.46 | $+10.40 |
| 2026-05-23 08:40:00 UTC | BTC | 5m | NO | $0.48 | $0.48 | +0.00 | $-9.65 | $-9.60 |
| 2026-05-23 08:45:00 UTC | ETH | 15m | NO | $0.48 | $0.45 | +0.03 | $-9.61 | $-9.90 |
| 2026-05-23 08:55:00 UTC | BTC | 5m | YES | $0.48 | $0.49 | -0.01 | $+10.40 | $+10.20 |
| 2026-05-23 09:05:00 UTC | ETH | 5m | YES | $0.48 | $0.48 | +0.00 | $-9.66 | $-9.60 |
| 2026-05-23 09:10:00 UTC | BTC | 5m | NO | $0.48 | $0.50 | -0.02 | $-9.62 | $-10.00 |
| 2026-05-23 09:25:00 UTC | BTC | 5m | YES | $0.48 | $0.49 | -0.01 | $+10.42 | $+10.20 |
| 2026-05-23 09:25:00 UTC | ETH | 5m | YES | $0.48 | $0.45 | +0.03 | $-9.61 | $-9.90 |
| 2026-05-23 09:45:00 UTC | BTC | 15m | YES | $0.48 | $0.51 | -0.03 | $-9.60 | $-9.69 |
