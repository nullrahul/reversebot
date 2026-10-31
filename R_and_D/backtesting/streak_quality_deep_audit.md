# 🔬 DEEP AUDIT: Streak Quality Score (June 1-2)

## 1. Timeframe Segmentation (All Hours)
### 5-Minute Markets
- **Baseline**: 282 trades | 54.6% WR | **$+330.83**
- **Filter PASS (>= 0.45)**: 155 trades | 54.8% WR | **$+189.06**
- **Filter BLOCKED (< 0.45)**: 127 trades | 54.3% WR | **$+141.77**
  - ✅ *Good Filter* (Blocked lower-quality trades)

### 15-Minute Markets
- **Baseline**: 98 trades | 54.1% WR | **$+105.96**
- **Filter PASS (>= 0.45)**: 59 trades | 54.2% WR | **$+64.20**
- **Filter BLOCKED (< 0.45)**: 39 trades | 53.8% WR | **$+41.76**
  - ✅ *Good Filter* (Blocked lower-quality trades)


## 2. Market Regime Segmentation (15m Markets Only)
### 🟢 SAFE HOURS (Optimized Regime)
- **Baseline**: 51 trades | 56.9% WR | **$+83.64**
- **Filter PASS (>= 0.45)**: 30 trades | 50.0% WR | **$+6.38**
- **Filter BLOCKED (< 0.45)**: 21 trades | 66.7% WR | **$+77.26**
  - ⚠️ *Bad Filter* (Blocked profitable setups)

### 🔴 TOXIC HOURS
- **Baseline**: 47 trades | 51.1% WR | **$+22.31**
- **Filter PASS (>= 0.45)**: 29 trades | 58.6% WR | **$+57.82**
- **Filter BLOCKED (< 0.45)**: 18 trades | 38.9% WR | **$-35.50**
  - ✅ *Good Filter* (Blocked lower-quality trades)
