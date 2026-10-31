# May 25, 2026 Mimic Verification & Backtest Report

Generated at: 2026-05-25 16:12:55 UTC
Target Audit Date: **2026-05-25**

## 📊 Executive Summary

This report performs a comprehensive trade-by-trade audit comparing the **V3 Backtester** against the **Actual Paper Trades** executed by the live Reversebot on the VPS for today, **May 25, 2026**. 

There are **NO manual closes** on May 25, providing a clean comparison window.

| Metric | Backtester (Simulated) | Live Bot (Actual Paper) | Discrepancy |
| --- | --- | --- | --- |
| **Total Trades** | 89 | 89 (3 still open) | +0 trades |
| **Wins** | 43 | 40 (of 86 resolved) | -3 wins |
| **Win Rate** | 48.31% | 46.51% | -1.80% |
| **Total P&L** | -42.73 USDC | -42.73 USDC | +0.00 USDC |

---

## 🔍 Mimic & Fidelity Audit

- **Total Matched Trades**: 89
- **Fidelity Match Rate (Backtest)**: 100.0% (Matched 89 / 89 simulated trades)
- **Fidelity Match Rate (Bot)**: 100.0% (Matched 89 / 89 actual trades)
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
*Count: 0*

Below is the distribution of the exact causes why the backtester/bot skipped or rejected potential signal candidates:

| Cause Category | Count | Description |
| --- | --- | --- |


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
*Count: 0*

None! Perfect matching.

---

## 📈 Detailed Matched Trade Sample

Below is a comparison of matched trades (first 25 shown):

| Time (UTC) | Asset | Window | Side | Entry (Sim) | Entry (Actual) | Price Diff | PnL (Sim) | PnL (Actual) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-25 00:10:00 UTC | BTC | 5m | YES | $0.66 | $0.66 | +0.00 | $+4.76 | $+4.76 |
| 2026-05-25 00:10:00 UTC | ETH | 5m | YES | $0.62 | $0.62 | +0.00 | $-4.34 | $-4.34 |
| 2026-05-25 00:30:00 UTC | BTC | 15m | YES | $0.54 | $0.54 | +0.00 | $-2.70 | $-2.70 |
| 2026-05-25 00:30:00 UTC | ETH | 15m | YES | $0.46 | $0.46 | +0.00 | $-2.30 | $-2.30 |
| 2026-05-25 01:00:00 UTC | ETH | 5m | NO | $0.50 | $0.50 | +0.00 | $+3.50 | $+3.50 |
| 2026-05-25 01:00:00 UTC | BTC | 5m | NO | $0.55 | $0.55 | +0.00 | $+2.25 | $+2.25 |
| 2026-05-25 01:15:00 UTC | ETH | 5m | YES | $0.55 | $0.55 | +0.00 | $-9.35 | $-9.35 |
| 2026-05-25 01:15:00 UTC | BTC | 15m | YES | $0.57 | $0.57 | +0.00 | $-2.85 | $-2.85 |
| 2026-05-25 01:15:00 UTC | ETH | 15m | YES | $0.45 | $0.45 | +0.00 | $-2.25 | $-2.25 |
| 2026-05-25 01:45:00 UTC | ETH | 5m | NO | $0.51 | $0.51 | +0.00 | $-2.55 | $-2.55 |
| 2026-05-25 02:10:00 UTC | ETH | 5m | YES | $0.50 | $0.50 | +0.00 | $-9.50 | $-9.50 |
| 2026-05-25 02:10:00 UTC | BTC | 5m | YES | $0.58 | $0.58 | +0.00 | $+2.52 | $+2.52 |
| 2026-05-25 02:25:00 UTC | ETH | 5m | NO | $0.73 | $0.73 | +0.00 | $+1.35 | $+1.35 |
| 2026-05-25 02:25:00 UTC | BTC | 5m | NO | $0.64 | $0.64 | +0.00 | $+1.80 | $+1.80 |
| 2026-05-25 02:45:00 UTC | ETH | 5m | YES | $0.49 | $0.49 | +0.00 | $-9.80 | $-9.80 |
| 2026-05-25 02:45:00 UTC | BTC | 5m | YES | $0.47 | $0.47 | +0.00 | $-3.29 | $-3.29 |
| 2026-05-25 02:45:00 UTC | ETH | 15m | NO | $0.57 | $0.57 | +0.00 | $+6.02 | $+6.02 |
| 2026-05-25 04:00:00 UTC | BTC | 15m | NO | $0.53 | $0.53 | +0.00 | $-3.18 | $-3.18 |
| 2026-05-25 04:15:00 UTC | ETH | 15m | NO | $0.55 | $0.55 | +0.00 | $-2.75 | $-2.75 |
| 2026-05-25 04:30:00 UTC | BTC | 5m | YES | $0.55 | $0.55 | +0.00 | $-11.55 | $-11.55 |
| 2026-05-25 04:30:00 UTC | ETH | 5m | YES | $0.51 | $0.51 | +0.00 | $-2.55 | $-2.55 |
| 2026-05-25 04:45:00 UTC | BTC | 15m | YES | $0.54 | $0.54 | +0.00 | $+2.30 | $+2.30 |
| 2026-05-25 04:45:00 UTC | ETH | 5m | YES | $0.46 | $0.46 | +0.00 | $+2.70 | $+2.70 |
| 2026-05-25 05:00:00 UTC | ETH | 15m | YES | $0.50 | $0.50 | +0.00 | $+2.50 | $+2.50 |
| 2026-05-25 05:15:00 UTC | BTC | 5m | YES | $0.57 | $0.57 | +0.00 | $-11.40 | $-11.40 |
