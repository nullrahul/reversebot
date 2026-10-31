# Streak Momentum Backtesting Report
Generated at: 2026-05-25 16:01:01 UTC

### Parameters:
- **Start Date**: 2026-05-01
- **End Date**: 2026-05-23
- **Symbols**: BTC, ETH
- **Entry Price Filter**: 0.45 to 0.65
- **Entry Method**: Option C (Binance first minute return estimation, coeff=5.0)


### 📈 Streak-2 (Current Live) Results

#### 📊 Baseline vs. Optimized Comparison
*Optimized regime excludes all weekends and high-toxicity session hours: ET Hours [1, 3, 4, 8, 11, 12, 15, 16, 17, 20, 22, 23]*

| Regime | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| 🔴 **Baseline (No Filters)** | 3378 | 46.4% | $-1704.36 | -5.0% |
| 🟢 **Optimized (No Weekends + Safe Hours)** | 1199 | 49.0% | $+44.74 | 0.4% |

#### 🟢 Optimized Segment Breakdown (Filter Active)
| Asset | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| BTC 5m | 514 | 50.4% | $+159.60 | +3.1% |
| BTC 15m | 157 | 52.2% | $+110.39 | +7.0% |
| ETH 5m | 441 | 46.5% | $-215.40 | -4.9% |
| ETH 15m | 87 | 48.3% | $-9.84 | -1.1% |

#### 🔴 Baseline: Weekend vs Weekday Split
| Regime | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| Weekday (Mon-Fri) | 2372 | 46.6% | $-1094.03 | -4.6% |
| Weekend (Sat-Sun) | 1006 | 45.9% | $-610.33 | -6.1% |

#### 🔴 Baseline: Entry Price Bucketing
| Bucket | Trades | Win Rate | PnL |
| --- | --- | --- | --- |
| $0.45-$0.49 | 3377 | 46.4% | $-1694.36 |
| $0.50-$0.54 | 1 | 0.0% | $-10.00 |

#### 🔴 Baseline: Hourly Performance (ET)
<details>
<summary>View Hourly Performance</summary>

| Hour (ET) | Trades | Win Rate | PnL |
| --- | --- | --- | --- |
| 00:00 | 141 | 50.4% | $+41.09 |
| 01:00 | 130 | 43.8% | $-134.61 |
| 02:00 | 135 | 48.1% | $-21.56 |
| 03:00 | 143 | 46.2% | $-80.32 |
| 04:00 | 144 | 45.1% | $-111.04 |
| 05:00 | 134 | 48.5% | $-11.93 |
| 06:00 | 144 | 50.0% | $+34.45 |
| 07:00 | 140 | 48.6% | $-8.51 |
| 08:00 | 142 | 40.8% | $-235.32 |
| 09:00 | 145 | 47.6% | $-36.16 |
| 10:00 | 151 | 49.7% | $+27.26 |
| 11:00 | 130 | 46.9% | $-52.33 |
| 12:00 | 143 | 38.5% | $-304.35 |
| 13:00 | 150 | 46.7% | $-67.97 |
| 14:00 | 142 | 48.6% | $-7.41 |
| 15:00 | 142 | 45.1% | $-111.03 |
| 16:00 | 129 | 45.0% | $-103.31 |
| 17:00 | 146 | 43.8% | $-152.26 |
| 18:00 | 143 | 49.7% | $+23.37 |
| 19:00 | 151 | 51.7% | $+85.20 |
| 20:00 | 136 | 41.9% | $-194.39 |
| 21:00 | 141 | 48.2% | $-17.29 |
| 22:00 | 146 | 38.4% | $-314.99 |
| 23:00 | 130 | 50.8% | $+49.05 |

</details>



### 📈 Streak-1 (1-Candle Momentum) Results

#### 📊 Baseline vs. Optimized Comparison
*Optimized regime excludes all weekends and high-toxicity session hours: ET Hours [0, 1, 4, 5, 8, 12, 14, 17, 20, 21, 23]*

| Regime | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| 🔴 **Baseline (No Filters)** | 5519 | 47.5% | $-1523.37 | -2.8% |
| 🟢 **Optimized (No Weekends + Safe Hours)** | 2090 | 47.8% | $-479.78 | -2.3% |

#### 🟢 Optimized Segment Breakdown (Filter Active)
| Asset | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| BTC 5m | 836 | 49.0% | $+26.35 | +0.3% |
| BTC 15m | 418 | 48.1% | $-68.20 | -1.6% |
| ETH 5m | 836 | 46.3% | $-437.93 | -5.2% |

#### 🔴 Baseline: Weekend vs Weekday Split
| Regime | Trades | Win Rate | PnL | ROI |
| --- | --- | --- | --- | --- |
| Weekday (Mon-Fri) | 3879 | 47.4% | $-1174.87 | -3.0% |
| Weekend (Sat-Sun) | 1640 | 47.9% | $-348.51 | -2.1% |

#### 🔴 Baseline: Entry Price Bucketing
| Bucket | Trades | Win Rate | PnL |
| --- | --- | --- | --- |
| $0.45-$0.49 | 5517 | 47.6% | $-1503.37 |
| $0.50-$0.54 | 2 | 0.0% | $-20.00 |

#### 🔴 Baseline: Hourly Performance (ET)
<details>
<summary>View Hourly Performance</summary>

| Hour (ET) | Trades | Win Rate | PnL |
| --- | --- | --- | --- |
| 00:00 | 230 | 47.8% | $-52.13 |
| 01:00 | 230 | 43.0% | $-276.04 |
| 02:00 | 230 | 47.0% | $-91.25 |
| 03:00 | 229 | 48.9% | $-0.30 |
| 04:00 | 230 | 47.8% | $-51.15 |
| 05:00 | 230 | 49.1% | $+9.49 |
| 06:00 | 230 | 48.3% | $-30.18 |
| 07:00 | 230 | 47.4% | $-70.35 |
| 08:00 | 230 | 47.0% | $-92.67 |
| 09:00 | 230 | 49.1% | $+14.97 |
| 10:00 | 230 | 49.1% | $+14.48 |
| 11:00 | 230 | 48.7% | $-6.92 |
| 12:00 | 230 | 51.3% | $+114.61 |
| 13:00 | 230 | 47.8% | $-48.96 |
| 14:00 | 230 | 42.2% | $-316.30 |
| 15:00 | 230 | 45.2% | $-173.37 |
| 16:00 | 230 | 49.6% | $+31.60 |
| 17:00 | 230 | 49.6% | $+28.59 |
| 18:00 | 230 | 47.4% | $-70.21 |
| 19:00 | 230 | 53.9% | $+234.50 |
| 20:00 | 230 | 44.8% | $-194.96 |
| 21:00 | 230 | 50.0% | $+54.08 |
| 22:00 | 230 | 45.7% | $-153.11 |
| 23:00 | 230 | 40.4% | $-397.82 |

</details>
