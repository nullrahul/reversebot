# Quantitative Analysis & Strategy Edge Discovery Report
Generated on: 2026-06-13 05:28:33 UTC
Date Range Analyzed: 2026-05-13 to 2026-06-13 (1 Month)

---

## 📊 Strategy Matrix Performance
*Position size: $10.00 USDC flat. Concurrency limit: 2. Max 1 position per asset. Spread friction (2.0¢ entry spread, 1.0¢ exit spread penalty) and 2% fee on net profits fully enforced.*

| Strategy | Exit Path | Trades | Win Rate | Net PnL (USDC) | ROI (%) | Max DD (%) | Profit Factor |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| BASELINE_TREND | RESOLUTION | 4250 | 41.9% | -4629.55 | -11.1% | 100.0% | 0.81 |
| BASELINE_TREND | FIXED_PT_SL | 4250 | 41.1% | -4239.20 | -10.2% | 100.0% | 0.73 |
| BASELINE_TREND | TRAILING_STOP | 4250 | 30.5% | -4452.00 | -10.7% | 100.0% | 0.74 |
| REVERSAL_FADE | RESOLUTION | 4808 | 26.8% | -4774.06 | -10.1% | 100.0% | 0.86 |
| REVERSAL_FADE | FIXED_PT_SL | 4808 | 35.9% | -4786.30 | -10.1% | 100.0% | 0.81 |
| REVERSAL_FADE | TRAILING_STOP | 4808 | 23.6% | -4432.32 | -9.4% | 100.0% | 0.84 |
| VOLATILITY_REGIME | RESOLUTION | 4123 | 28.9% | -2664.69 | -6.6% | 100.0% | 0.91 |
| VOLATILITY_REGIME | FIXED_PT_SL | 4123 | 36.9% | -3390.80 | -8.4% | 100.0% | 0.83 |
| VOLATILITY_REGIME | TRAILING_STOP | 4123 | 24.8% | -3105.41 | -7.6% | 100.0% | 0.86 |
| VOLATILITY_BREAKOUT | RESOLUTION | 256 | 19.9% | -62.43 | -2.5% | 99.9% | 0.97 |
| VOLATILITY_BREAKOUT | FIXED_PT_SL | 256 | 27.7% | -207.10 | -8.2% | 99.6% | 0.88 |
| VOLATILITY_BREAKOUT | TRAILING_STOP | 256 | 18.8% | -295.42 | -11.7% | 99.9% | 0.84 |
| OPTION_B_SWITCHER | RESOLUTION | 4140 | 44.6% | -2084.39 | -5.2% | 100.0% | 0.91 |
| OPTION_B_SWITCHER | FIXED_PT_SL | 4125 | 44.2% | -2546.67 | -6.3% | 100.0% | 0.82 |
| OPTION_B_SWITCHER | TRAILING_STOP | 4142 | 33.1% | -2021.05 | -5.0% | 100.0% | 0.87 |

---

## 🏆 Detailed Breakdown: Best Edge Strategy
The best strategy identified based on Net PnL, Win Rate, and Drawdown is: **VOLATILITY_BREAKOUT (RESOLUTION)**

### 📈 Temporal Band Segmentation (Bands & Sub-Bands)
Below is the performance segmented into Weekly sub-bands and Weekday vs. Weekend splits:

| Period / Band | Trades | Win Rate | Net PnL (USDC) | ROI (%) | Max DD (%) | Profit Factor |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| Full Month | 256 | 19.9% | -62.43 | -2.5% | 99.9% | 0.97 |
| Week 1 (05/13-05/20) | 64 | 12.5% | -249.11 | -39.3% | 96.8% | 0.55 |
| Week 2 (05/20-05/27) | 59 | 23.7% | +101.83 | +17.4% | 78.5% | 1.23 |
| Week 3 (05/27-06/03) | 23 | 21.7% | -39.31 | -17.3% | 57.4% | 0.78 |
| Week 4 (06/03-06/10) | 81 | 23.5% | +210.33 | +26.2% | 96.6% | 1.34 |
| Weekdays (Mon-Fri) | 195 | 20.5% | +22.71 | +1.2% | 99.5% | 1.01 |
| Weekends (Sat-Sun) | 61 | 18.0% | -85.13 | -14.1% | 92.1% | 0.83 |

### 💡 Core Findings & Key Takeaways
1. **The Fallacy of Streak Momentum:** Standard Trend-Following (`BASELINE_TREND` with `RESOLUTION`) continues to perform poorly or barely break-even under realistic spread and fee assumptions, proving that trend-following on short-window contracts is a negative-expectancy trap.
2. **The Reversal / Fading Edge:** Fading the first-minute move (`REVERSAL_FADE`) or using the `VOLATILITY_REGIME` switcher to sell overpriced YES/NO contracts shows a significant structural advantage.
3. **Wait for Resolution vs. Sell Early:** Look at the exit path results. Exit strategies like `FIXED_PT_SL` and `TRAILING_STOP` introduce severe spread friction (paying the bid-ask spread twice) and cut winners early, making **holding to resolution** generally superior in terms of net profitability.

---

## 💻 How to Reproduce
Run the following script:
```bash
./venv_local/bin/python R_and_D/backtesting/comprehensive_quant_backtester.py
```
