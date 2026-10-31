# May 24, 2026 Mimic Verification & Backtest Report

Generated at: 2026-05-24 08:50:28 UTC
Target Audit Date: **2026-05-24**

## 📊 Executive Summary

This report performs a comprehensive trade-by-trade audit comparing the **V3 Backtester** against the **Actual Paper Trades** executed by the live Reversebot on the VPS for today, **May 24, 2026**. 

There are **NO manual closes** on May 24, providing a clean comparison window.

| Metric | Backtester (Simulated) | Live Bot (Actual Paper) | Discrepancy |
| --- | --- | --- | --- |
| **Total Trades** | 50 | 50 (2 still open) | +0 trades |
| **Wins** | 27 | 25 (of 48 resolved) | -2 wins |
| **Win Rate** | 54.00% | 52.08% | -1.92% |
| **Total P&L** | +25.48 USDC | +25.48 USDC | +0.00 USDC |

---

## 🔍 Mimic & Fidelity Audit

- **Total Matched Trades**: 50
- **Fidelity Match Rate (Backtest)**: 100.0% (Matched 50 / 50 simulated trades)
- **Fidelity Match Rate (Bot)**: 100.0% (Matched 50 / 50 actual trades)
- **Win/Loss Resolution Mismatches**: 0
- **Average Entry Price Discrepancy (Simulated - Actual)**: +0.0000
- **Mean Absolute Error (MAE) on Entry Price**: 0.0000 USDC
- **Mean Absolute Error (MAE) on Trade P&L**: 0.00 USDC

### ⚖️ Verdict: Does the Backtester Mimic the Bot?

> [!NOTE]
> **Fidelity Verdict**: **HIGH FIDELITY (>= 80% Match)**
> 
> **Fidelity Match Rate (Bot)** is **100.00%**.
> **Option C Price Simulation MAE**: **0.0000**.

---

## 🔬 Discrepancy Cause Breakdown

### 1. Why Did the Bot Skip Potential Signal Candidates? (Rejections & Skips)
*Count: 74*

Below is the distribution of the exact causes why the backtester/bot skipped or rejected potential signal candidates:

| Cause Category | Count | Description |
| --- | --- | --- |
| **Other Scanner/Execution Mismatch** | 53 | Scanner or Execution layer differences. |
| **Bot Rejection: Asset Cap Reached** | 21 | Live bot hit exposure/asset cap and rejected signal. |

### 2. Why Did the Backtest Skip Bot Trades? (Missing in Backtest)
*Count: 0*

Below is the distribution of the exact causes why the backtester missed trades that the live bot actually executed:

| Cause Category | Count | Description |
| --- | --- | --- |


---

## 📋 Detailed Discrepancy Analysis

### ⚠️ Bot Trades NOT Found in Backtest (Missing in Backtest)
*Count: 0*

None! Perfect matching.

### ⚠️ Extra Executed Trades in Backtester (Extra in Backtest)
*Count: 0*

None! Perfect mimic.

### ⚠️ Skipped/Rejected Signal Candidates (Why they did not trade)
*Count: 74*

| Time (UTC) | Asset | Window | Side | Entry (Sim) | PnL (Sim) | Cause |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-05-24 00:00:00 UTC | BTC | 5m | YES | $0.48 | $-9.54 | Quiet Skip (Unprocessed by bot) |
| 2026-05-24 00:00:00 UTC | BTC | 15m | YES | $0.48 | $-9.54 | Bot Rejected: Asset Cap reached for bitcoin |
| 2026-05-24 00:05:00 UTC | ETH | 5m | NO | $0.48 | $+10.43 | Streak Reset Guard active (last close: 1779581100) |
| 2026-05-24 00:10:00 UTC | ETH | 5m | NO | $0.48 | $-9.62 | Streak Reset Guard active (last close: 1779581100) |
| 2026-05-24 00:20:00 UTC | BTC | 5m | YES | $0.48 | $-9.57 | Streak Reset Guard active (last close: 1779581700) |
| 2026-05-24 00:25:00 UTC | ETH | 5m | NO | $0.48 | $-9.58 | Asset Cap reached for eth |
| 2026-05-24 00:30:00 UTC | ETH | 15m | NO | $0.48 | $-9.64 | Streak Reset Guard active (last close: 1779582600) |
| 2026-05-24 00:35:00 UTC | BTC | 5m | YES | $0.48 | $+10.42 | Asset Cap reached for btc |
| 2026-05-24 00:40:00 UTC | BTC | 5m | YES | $0.48 | $-9.57 | Asset Cap reached for btc |
| 2026-05-24 00:40:00 UTC | ETH | 5m | YES | $0.48 | $-9.58 | Streak Reset Guard active (last close: 1779583200) |
| 2026-05-24 01:00:00 UTC | BTC | 15m | YES | $0.48 | $-9.55 | Streak Reset Guard active (last close: 1779583500) |
| 2026-05-24 01:05:00 UTC | ETH | 5m | YES | $0.48 | $-9.66 | Asset Cap reached for eth |
| 2026-05-24 01:15:00 UTC | ETH | 5m | NO | $0.48 | $-9.58 | Bot Rejected: Asset Cap reached for ethereum |
| 2026-05-24 01:15:00 UTC | ETH | 15m | YES | $0.48 | $+10.38 | Streak Reset Guard active (last close: 1779585300) |
| 2026-05-24 01:25:00 UTC | BTC | 5m | YES | $0.48 | $-9.62 | Streak Reset Guard active (last close: 1779585900) |
| 2026-05-24 01:30:00 UTC | ETH | 15m | YES | $0.48 | $-9.68 | Streak Reset Guard active (last close: 1779585300) |
| 2026-05-24 01:35:00 UTC | ETH | 5m | NO | $0.48 | $-9.58 | Streak Reset Guard active (last close: 1779586200) |
| 2026-05-24 01:45:00 UTC | BTC | 5m | NO | $0.48 | $-9.58 | Streak Reset Guard active (last close: 1779586800) |
| 2026-05-24 02:10:00 UTC | ETH | 5m | YES | $0.48 | $+10.41 | Streak Reset Guard active (last close: 1779588600) |
| 2026-05-24 02:15:00 UTC | ETH | 5m | YES | $0.48 | $-9.62 | Streak Reset Guard active (last close: 1779588600) |
| 2026-05-24 02:20:00 UTC | BTC | 5m | YES | $0.48 | $-9.63 | Asset Cap reached for btc |
| 2026-05-24 02:25:00 UTC | ETH | 5m | NO | $0.48 | $-9.60 | Asset Cap reached for eth |
| 2026-05-24 02:40:00 UTC | BTC | 5m | YES | $0.48 | $+10.40 | Streak Reset Guard active (last close: 1779590400) |
| 2026-05-24 02:40:00 UTC | ETH | 5m | YES | $0.48 | $+10.42 | Streak Reset Guard active (last close: 1779590400) |
| 2026-05-24 02:45:00 UTC | BTC | 5m | YES | $0.48 | $-9.60 | Streak Reset Guard active (last close: 1779590400) |
| 2026-05-24 02:45:00 UTC | ETH | 5m | YES | $0.48 | $-9.61 | Streak Reset Guard active (last close: 1779590400) |
| 2026-05-24 03:15:00 UTC | BTC | 5m | NO | $0.48 | $+10.44 | Bot Rejected: Asset Cap reached for bitcoin |
| 2026-05-24 03:15:00 UTC | BTC | 15m | YES | $0.48 | $-9.64 | Streak Reset Guard active (last close: 1779592500) |
| 2026-05-24 03:20:00 UTC | ETH | 5m | NO | $0.48 | $-9.62 | Streak Reset Guard active (last close: 1779592800) |
| 2026-05-24 03:40:00 UTC | BTC | 5m | NO | $0.48 | $+10.39 | Streak Reset Guard active (last close: 1779594000) |
| 2026-05-24 03:40:00 UTC | ETH | 5m | NO | $0.48 | $+10.41 | Streak Reset Guard active (last close: 1779593700) |
| 2026-05-24 03:45:00 UTC | BTC | 5m | NO | $0.48 | $-9.63 | Streak Reset Guard active (last close: 1779594000) |
| 2026-05-24 03:45:00 UTC | ETH | 15m | NO | $0.48 | $-9.65 | Asset Cap reached for eth |
| 2026-05-24 03:55:00 UTC | ETH | 5m | YES | $0.48 | $-9.61 | Streak Reset Guard active (last close: 1779594600) |
| 2026-05-24 04:00:00 UTC | BTC | 5m | NO | $0.48 | $-9.70 | Bot Rejected: Asset Cap reached for bitcoin |
| 2026-05-24 04:00:00 UTC | BTC | 15m | NO | $0.48 | $-9.70 | Streak Reset Guard active (last close: 1779595200) |
| 2026-05-24 04:20:00 UTC | ETH | 5m | YES | $0.48 | $-9.60 | Asset Cap reached for eth |
| 2026-05-24 04:30:00 UTC | BTC | 5m | NO | $0.48 | $+10.39 | Streak Reset Guard active (last close: 1779597000) |
| 2026-05-24 04:30:00 UTC | ETH | 5m | NO | $0.48 | $-9.61 | Bot Rejected: Asset Cap reached for ethereum |
| 2026-05-24 04:35:00 UTC | BTC | 5m | NO | $0.48 | $+10.38 | Streak Reset Guard active (last close: 1779597000) |
| 2026-05-24 04:50:00 UTC | BTC | 5m | YES | $0.48 | $+10.40 | Streak Reset Guard active (last close: 1779597900) |
| 2026-05-24 05:05:00 UTC | BTC | 5m | NO | $0.48 | $+10.44 | Streak Reset Guard active (last close: 1779598800) |
| 2026-05-24 05:10:00 UTC | BTC | 5m | NO | $0.48 | $+10.43 | Asset Cap reached for btc |
| 2026-05-24 05:15:00 UTC | BTC | 5m | NO | $0.48 | $+10.42 | Bot Rejected: Asset Cap reached for bitcoin |
| 2026-05-24 05:15:00 UTC | ETH | 5m | NO | $0.48 | $+10.43 | Bot Rejected: NO price (0.68) exceeds maximum allowed (0.6615) |
| 2026-05-24 05:20:00 UTC | ETH | 5m | NO | $0.48 | $+10.40 | Asset Cap reached for eth |
| 2026-05-24 05:25:00 UTC | BTC | 5m | NO | $0.48 | $+10.43 | Streak Reset Guard active (last close: 1779600300) |
| 2026-05-24 05:25:00 UTC | ETH | 5m | NO | $0.48 | $-9.56 | Asset Cap reached for eth |
| 2026-05-24 05:30:00 UTC | BTC | 5m | NO | $0.48 | $-9.63 | Streak Reset Guard active (last close: 1779600300) |
| 2026-05-24 05:30:00 UTC | BTC | 15m | NO | $0.48 | $+10.37 | Streak Reset Guard active (last close: 1779599700) |
| 2026-05-24 05:30:00 UTC | ETH | 15m | NO | $0.48 | $+10.38 | Streak Reset Guard active (last close: 1779600600) |
| 2026-05-24 05:45:00 UTC | BTC | 15m | NO | $0.48 | $-9.62 | Asset Cap reached for btc |
| 2026-05-24 05:45:00 UTC | ETH | 5m | NO | $0.48 | $-9.65 | Streak Reset Guard active (last close: 1779601500) |
| 2026-05-24 05:45:00 UTC | ETH | 15m | NO | $0.48 | $-9.65 | Streak Reset Guard active (last close: 1779600600) |
| 2026-05-24 05:55:00 UTC | BTC | 5m | YES | $0.48 | $-9.61 | Streak Reset Guard active (last close: 1779601800) |
| 2026-05-24 06:00:00 UTC | ETH | 5m | YES | $0.48 | $+11.02 | Streak Reset Guard active (last close: 1779602400) |
| 2026-05-24 06:05:00 UTC | ETH | 5m | YES | $0.48 | $+10.43 | Streak Reset Guard active (last close: 1779602400) |
| 2026-05-24 06:15:00 UTC | BTC | 5m | YES | $0.48 | $-9.66 | Streak Reset Guard active (last close: 1779603300) |
| 2026-05-24 06:15:00 UTC | BTC | 15m | YES | $0.48 | $-9.66 | Quiet Skip (Unprocessed by bot) |
| 2026-05-24 06:15:00 UTC | ETH | 15m | YES | $0.48 | $-9.65 | Bot Rejected: Asset Cap reached for ethereum |
| 2026-05-24 06:20:00 UTC | ETH | 5m | NO | $0.48 | $+10.39 | Streak Reset Guard active (last close: 1779603300) |
| 2026-05-24 06:30:00 UTC | BTC | 5m | NO | $0.48 | $+10.38 | Streak Reset Guard active (last close: 1779604200) |
| 2026-05-24 06:35:00 UTC | BTC | 5m | NO | $0.48 | $+10.38 | Streak Reset Guard active (last close: 1779604200) |
| 2026-05-24 06:40:00 UTC | BTC | 5m | NO | $0.48 | $-9.61 | Quiet Skip (Unprocessed by bot) |
| 2026-05-24 06:50:00 UTC | ETH | 5m | YES | $0.48 | $+10.47 | Streak Reset Guard active (last close: 1779605400) |
| 2026-05-24 06:55:00 UTC | BTC | 5m | YES | $0.48 | $-9.61 | Streak Reset Guard active (last close: 1779605700) |
| 2026-05-24 06:55:00 UTC | ETH | 5m | YES | $0.48 | $+10.42 | Streak Reset Guard active (last close: 1779605400) |
| 2026-05-24 07:00:00 UTC | ETH | 15m | YES | $0.48 | $-9.60 | Asset Cap reached for eth |
| 2026-05-24 07:05:00 UTC | ETH | 5m | YES | $0.48 | $-9.70 | Streak Reset Guard active (last close: 1779606300) |
| 2026-05-24 07:30:00 UTC | BTC | 15m | NO | $0.48 | $+10.44 | Streak Reset Guard active (last close: 1779606900) |
| 2026-05-24 07:40:00 UTC | ETH | 5m | NO | $0.48 | $+10.43 | Asset Cap reached for eth |
| 2026-05-24 07:45:00 UTC | BTC | 15m | NO | $0.48 | $+10.43 | Quiet Skip (Unprocessed by bot) |
| 2026-05-24 07:45:00 UTC | ETH | 15m | NO | $0.48 | $+10.45 | Streak Reset Guard active (last close: 1779608700) |
| 2026-05-24 07:50:00 UTC | ETH | 5m | NO | $0.48 | $-9.63 | Streak Reset Guard active (last close: 1779609000) |

---

## 📈 Detailed Matched Trade Sample

Below is a comparison of matched trades (first 25 shown):

| Time (UTC) | Asset | Window | Side | Entry (Sim) | Entry (Actual) | Price Diff | PnL (Sim) | PnL (Actual) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-24 00:00:00 UTC | ETH | 5m | NO | $0.41 | $0.41 | +0.00 | $+14.16 | $+14.16 |
| 2026-05-24 00:10:00 UTC | BTC | 5m | NO | $0.56 | $0.56 | +0.00 | $-9.52 | $-9.52 |
| 2026-05-24 00:15:00 UTC | ETH | 15m | NO | $0.49 | $0.49 | +0.00 | $+10.20 | $+10.20 |
| 2026-05-24 00:30:00 UTC | BTC | 15m | NO | $0.55 | $0.55 | +0.00 | $-9.90 | $-9.90 |
| 2026-05-24 00:35:00 UTC | ETH | 5m | YES | $0.50 | $0.50 | +0.00 | $+10.00 | $+10.00 |
| 2026-05-24 01:00:00 UTC | ETH | 15m | YES | $0.48 | $0.48 | +0.00 | $+10.40 | $+10.40 |
| 2026-05-24 01:05:00 UTC | BTC | 5m | YES | $0.49 | $0.49 | +0.00 | $-9.80 | $-9.80 |
| 2026-05-24 01:20:00 UTC | BTC | 5m | YES | $0.54 | $0.54 | +0.00 | $+8.28 | $+8.28 |
| 2026-05-24 01:25:00 UTC | ETH | 5m | YES | $0.46 | $0.46 | +0.00 | $-9.66 | $-9.66 |
| 2026-05-24 01:35:00 UTC | BTC | 5m | YES | $0.48 | $0.48 | +0.00 | $-9.60 | $-9.60 |
| 2026-05-24 02:05:00 UTC | BTC | 5m | YES | $0.47 | $0.47 | +0.00 | $+11.13 | $+11.13 |
| 2026-05-24 02:05:00 UTC | ETH | 5m | YES | $0.47 | $0.47 | +0.00 | $+11.13 | $+11.13 |
| 2026-05-24 02:15:00 UTC | BTC | 15m | YES | $0.46 | $0.46 | +0.00 | $-9.66 | $-9.66 |
| 2026-05-24 02:15:00 UTC | ETH | 15m | YES | $0.46 | $0.46 | +0.00 | $-9.66 | $-9.66 |
| 2026-05-24 02:35:00 UTC | BTC | 5m | YES | $0.45 | $0.45 | +0.00 | $+12.10 | $+12.10 |
| 2026-05-24 02:35:00 UTC | ETH | 5m | YES | $0.51 | $0.51 | +0.00 | $+9.31 | $+9.31 |
| 2026-05-24 03:00:00 UTC | BTC | 15m | YES | $0.47 | $0.47 | +0.00 | $+11.13 | $+11.13 |
| 2026-05-24 03:15:00 UTC | ETH | 5m | NO | $0.51 | $0.51 | +0.00 | $+9.31 | $+9.31 |
| 2026-05-24 03:20:00 UTC | BTC | 5m | NO | $0.47 | $0.47 | +0.00 | $-9.87 | $-9.87 |
| 2026-05-24 03:30:00 UTC | ETH | 5m | YES | $0.52 | $0.52 | +0.00 | $-9.88 | $-9.88 |
| 2026-05-24 03:35:00 UTC | BTC | 5m | NO | $0.45 | $0.45 | +0.00 | $+12.10 | $+12.10 |
| 2026-05-24 03:45:00 UTC | BTC | 15m | NO | $0.45 | $0.45 | +0.00 | $+12.10 | $+12.10 |
| 2026-05-24 03:45:00 UTC | ETH | 5m | NO | $0.42 | $0.42 | +0.00 | $-9.66 | $-9.66 |
| 2026-05-24 04:00:00 UTC | ETH | 5m | NO | $0.21 | $0.21 | +0.00 | $-9.87 | $-9.87 |
| 2026-05-24 04:15:00 UTC | ETH | 15m | YES | $0.50 | $0.50 | +0.00 | $-10.00 | $-10.00 |
