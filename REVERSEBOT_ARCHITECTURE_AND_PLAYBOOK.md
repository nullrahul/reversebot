# ♊ Polymarket ReverseBot (RuFlo V3) — Comprehensive Architecture & Strategic Playbook

> [!IMPORTANT]
> **AI Context Anchor & Guardrails**: This document is the absolute source of truth for the technical and quantitative architecture of the Polymarket ReverseBot (RuFlo V3). Any incoming AI agent must read this file in full before proposing or executing changes to the codebase, configuration, or trading strategy.

---

## 1. Core Concept & Streak Momentum Strategy

Polymarket ReverseBot (RuFlo V3) is a high-performance algorithmic trading engine designed for Polymarket's high-frequency **crypto short-window "Up or Down" micro-markets** (5-minute and 15-minute intervals).

```
[Candle T-2] ──▶ [Candle T-1] ──▶ [Streak Fired] ──▶ [Validation Gates] ──▶ [Polymarket Exec]
 (UP or DOWN)     (UP or DOWN)    (Match detected)      (CVD, Spread, etc)     (YES or NO Token)
```

### The Strategy Logic
The bot tracks the direction of completed price candles on Binance (e.g. close > open = `UP`, close < open = `DOWN`) across two specific historical lookbacks:
- **T-1 Candle**: The most recently completed contract window.
- **T-2 Candle**: The contract window preceding T-1.

A **Streak Momentum Signal** is generated when both lookback candles resolve in the same direction:
- **UP - UP Streak**: Strong upward momentum. The bot triggers a **YES** buy signal (expecting the trend to continue).
- **DOWN - DOWN Streak**: Strong downward momentum. The bot triggers a **NO** buy signal (betting that the market resolves as `DOWN`, which makes the `NO` token payout equal to `$1.00`).

---

## 2. Pre-Entry Validation Gates & Guards

Once a streak signal is detected, it must pass through **all 10 Validation Gates** inside the engine (`src/strategy/validator.py`) and the tactical **Decision Engine** (`src/strategy/decision.py`) before routing to execution.

| Check | Gate Name | Active Condition | R&D Value | Strategic Purpose |
|:---|:---|:---|:---|:---|
| **1** | **Liquidity Check** | `volume_24h >= min_volume` | `$500.00 USDC` | **Bypassed for short-window contracts**. Since 5m/15m contracts are created dynamically, they have $0 volume at spawn; enforcing a floor would block 100% of entries. |
| **2** | **Spread Guard** | `spread_pct <= max_spread` | `60.0%` of ask | Protects against thin, illiquid orderbooks and massive slippage. |
| **3** | **Time Check** | `seconds_to_close >= min_secs` | `60s` (5m) / `120s` (15m) | Prevents entering too close to contract expiration (avoids settlement locks). |
| **4** | **Duplicate Guard** | `has_open_position == False` | `True` (Strict) | Prevents double-entering or piling into duplicate contract IDs. |
| **5** | **Exposure Guard** | `net_exposure <= max_exposure` | `$15.00 USDC` | Limits global capital risked at any single point in time. |
| **6** | **Daily Loss Guard** | `daily_realised_loss < limit` | `$75.00 USDC` | Emergency circuit breaker to halt trading on catastrophic regimes. |
| **7** | **Position Count** | `open_count < max_positions` | `15` concurrent | Prevents thread over-allocation and systemic capital lock up. |
| **8** | **Price Stability** | `consecutive_cycles >= required` | `True` (Relaxed for crypto) | Assures price confirmation cycles before routing orders. |
| **9** | **Category Filter** | `category in whitelist` | `True` (Bypassed for crypto) | Restricts non-crypto markets to specific approved categories. |
| **10** | **Spread Vacuum** | `wick_guard_active` | `True` (Bypassed in V3) | Flash crash/wick protector; bypassed for streak momentum. |

### Advanced Tactical Decision Gates
Beyond basic validators, the bot evaluates three **Quantitative overlays** which act as ultimate draw-down filters:

#### A. CVD Contradiction Gate ("The Ultimate Friend")
* **Concept**: Compares book order flow against Binance Cumulative Volume Delta (CVD) to verify institutional buying/selling power.
* **YES Side**: Requires positive CVD momentum; blocks entry if order book is buying but net volume delta shows negative spot divergence.
* **NO Side**: Enforces negative CVD momentum.
* **Performance Impact**: Audited as our most powerful filter—blocking this gate alone saved **-$378.70 USDC** in chopper drawdowns over a single session (avoiding toxic whipsaws).

#### B. Orderbook Imbalance Gate ("The Super Friend")
* **Concept**: Evaluates the bid-ask volume depth in the CLOB book.
* **Logic**: If trading NO, requires bid depth support. Blocks entries if buyers/sellers heavily dominate the opposing side (indicating institutional block-orders are front-running the close).
* **Performance Impact**: Saved **-$280.06 USDC** in chops by catching book imbalance traps.

#### C. NO Token Price Entry Ceiling ($0.55 Cap)
* **Concept**: Never pay a premium for NO tokens.
* **Logic**: If the price of NO token exceeds **$0.55** (55¢), entry is strictly rejected.
* **Why**: At entry prices > $0.55, the risk-reward ratio mathematically destroys the strategy's expected value (EV) under standard binance volatility, turning the position into a low-probability, low-odds trap.

#### D. Weekend Lockout
* **Lockout Period**: **Friday 5:00 PM ET to Sunday 6:00 PM ET** (Asian open/weekly trend open is allowed).
* **Why**: Weekend trading regimes are highly toxic (-6.2% ROI), characterized by extremely low retail volume, high oracle settlement latency, and high-manipulation wicks.

---

## 3. Strategic Mathematics & Position Sizing

### Half-Kelly Position Sizing
Position sizing is dynamically calculated using a **Half-Kelly multiplier** to prevent capital ruin while maximizing compounding growth.
$$\text{Kelly Fraction} = p - \frac{1 - p}{b}$$
$$\text{Half-Kelly} = \max\left(0.0, \, \text{Kelly Fraction} \times 0.5\right)$$
* **$p$ (Win Probability)**: Queried from real-time rolling win rates in the `signals` database, solving the cold-start sparsity problem.
* **$b$ (Net Odds)**: Calculated as $\frac{1.0}{\text{Entry Price}} - 1.0$.

### Correlated Asset Sizing Penalty
If the bot has an open position on a correlated ticker (e.g. BTC is open, and a new signal fires on ETH):
* **Temporal Check**: Only scales down if both positions share the **same timeframe window** (e.g. both are 15m).
* **Penalty**: Multiplies the new position's size by **0.5x (50% reduction)**. This limits systemic exposure if the entire crypto market experiences a correlated flash crash.

### Take Profit (TP) & Trailing Splits
* **TP Multiplier**: Triggers full/partial close when position value reaches **2.0x cost basis** (100% gain).
* **Trailing TP**: Uses a **60/40 trailing split** (closes 60% of shares at target, lets remaining 40% run with a trailing stop to capture extreme momentum spikes).

---

## 4. The Critical Strategic Imperatives

### Mandate 1: STRICTLY NO STOP LOSS
Never apply a traditional Stop Loss (e.g., cutting at -48% or -50%) to 5m/15m Polymarket binary options. 
* **The Math**: Micro-market binary options are subject to extreme, temporary wicks (often hitting -97% drawdown in the first minutes) before reverting and settling at $1.00 (full payout). 
* **The Destruction**: Cutting losses early destroys the strategy. Saving a fraction of capital on losers is mathematically outweighed by the destruction of full-payout winning trades that merely touched a brief negative wick. Risk is managed **strictly and only** via Kelly position sizing and take-profit limits.

### Mandate 2: Streak Reset Overlap Guard
Once a trade is executed at contract `closes_at` time $T$, the bot must wait for a completely fresh, non-overlapping streak sequence:
$$\text{Lookback 1 Close Time} > T$$
This ensures the bot never trades on a candle sequence that was part of a previously executed trade, preventing momentum exhaustion traps.

---

## 5. Friction & Fee Structure

```
Gross Traded Payout ──▶ [Friction Simulation: 0.98 FEE_MULTIPLIER] ──▶ Conservative Dashboard Stats
```

### The "Why" Behind the Fee Math
* **The Reality**: Polymarket CLOB has **0% trading and settlement fees** (`FEE_MULTIPLIER = 1.0` in live execution).
* **The Simulation**: The database, expected value (EV) calculations, and paper trading telemetry enforce a **2% fee simulation** (`FEE_MULTIPLIER = 0.98` on net winnings).
* **Reasoning**:
  1. **Slippage Buffer**: Models execution slippage and spread friction in fast-moving markets.
  2. **Conservative Stats**: Guarantees that paper trading statistics displayed on the dashboard are highly conservative and never overstate real-world live execution returns.

---

## 6. Self-Healing Database & Telemetry

The SQLite database (`reversebot_rd.db`) uses **WAL (Write-Ahead Logging)** mode for high-concurrency read-performance.

### Self-Healing Expired Positions
* **The Problem**: Polymarket Daily/Weekly contracts settle at extreme fractional prices (e.g. `0.9995` / `0.0005`) on the Gamma API, but their official settlement state stays `closed: False` and `active: True` for hours awaiting oracle clearance.
* **The Healing Logic**: In `src/strategy/position_mgr.py`, the engine intercepts expired contracts (current time > `closes_at`) and applies:
  - If target price $\ge 0.98$ $\rightarrow$ Settles immediately as a **Win** (exit price = `1.0`).
  - If target price $\le 0.02$ or opposing $\ge 0.98$ $\rightarrow$ Settles immediately as a **Loss** (exit price = `0.0`).
* **Benefit**: Instantly frees locked paper capital, cleans downstream rolling Kelly metrics, and maintains dashboard accuracy.

---

## 7. Next-Phase Strategic Advisory (For Incoming AI Agents)

When advising on the next development phases, target these high-value objectives:
1. **Regime-Shifting Kelly Calibration**: Implement a dynamic multiplier that automatically shifts from Half-Kelly to Quarter-Kelly when market volatility regimes switch to "CHOP" (detected via Binance rolling ATR).
2. **Machine Learning Signal Score**: Train a local light gradient-boosted tree (XGBoost/LightGBM) on the snapshot signals database (`signals` table) to output a probability score based on CVD, spread, time-to-close, and hourly ET.
3. **Dynamic Bid-Ask Slippage Modeling**: Adjust entry premium calculations using a sliding scale based on best ask depth, rather than a conservative flat midpoint.
