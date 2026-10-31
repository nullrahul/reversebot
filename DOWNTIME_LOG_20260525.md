# ♊ Polymarket ReverseBot (RuFlo V3) — Operational Downtime Log

Use the parameters in this log to synchronize your backtesting engine and raw telemetry analysis for the **May 25–26, 2026** session. By applying this time filter, you can ensure that the backtest simulation perfectly mirrors the live bot's actual trading availability.

---

## 📅 Lockout Window Parameters

| Parameter | Value (Eastern Time - ET) | Value (Indian Standard Time - IST) | Unix Timestamp (UTC) |
|:---|:---|:---|:---|
| **Downtime Start** | **Monday, May 25, 2026, 3:50:01 PM** | **Tuesday, May 26, 2026, 1:20:01 AM** | `1779738601` |
| **Downtime End** | **Monday, May 25, 2026, 11:16:25 PM** | **Tuesday, May 26, 2026, 8:46:25 AM** | `1779765385` |

### ⏱️ Total Downtime Duration
* **Time Format**: **7 Hours, 26 Minutes, 24 Seconds**
* **Total Seconds**: **`26,784 seconds`**
* **Total 15-Minute Candles Affected**: **30 Candles** (from `15:45-16:00 ET` up to `23:00-23:15 ET`)
* **Total 5-Minute Candles Affected**: **89 Candles**

---

## 🔍 Root Cause & Trigger Sequence

1. **Reset & Cold-Start**: Database was reset at **3:20 PM ET** (starting realized P&L at `$0.00`, defaulting starting paper bankroll to `$100.00 USDC`).
2. **Execution into Toxicity**: Bot entered 7 consecutive paper trades between **3:23 PM ET** and **3:50 PM ET** (during the known toxic US market close whipsaw window).
3. **Breaker Tripped**: The 7 trades resulted in full paper losses totaling **`-$27.61 USDC`** (27.61% drawdown on the fresh `$100` balance).
4. **Halt Action**: The `drawdown >= 20%` breaker was tripped immediately on the 7th trade close at **3:50:01 PM ET**, setting `drawdown_halted = True` in memory and blocking 100% of all subsequent trade evaluations.
5. **Recovery**: PM2 process restarted with the new `$10,000.00 USDC` default paper balance configuration, clearing the halt state at **11:16:25 PM ET**.

---

## 🛠️ Backtester Alignment Guide

To make your backtester matches identical for today, apply the following exclusion logic in your Python simulation script:

```python
# Apply operational downtime filter for May 25-26, 2026 session
DOWNTIME_START = 1779738601  # Mon May 25 15:50:01 ET
DOWNTIME_END   = 1779765385  # Mon May 25 23:16:25 ET

def should_simulate_trade(signal_timestamp_unix):
    if DOWNTIME_START <= signal_timestamp_unix <= DOWNTIME_END:
        return False  # Exclude: Live bot was locked out in drawdown halt
    return True
```

Alternatively, you can query your results directly via SQL:
```sql
SELECT * FROM signals 
WHERE signal_timestamp < '2026-05-25T19:50:01.000Z' 
   OR signal_timestamp > '2026-05-26T03:16:25.000Z';
```
