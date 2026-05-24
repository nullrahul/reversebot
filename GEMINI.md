# ♊ Gemini Project Instructions: Polymarket ReverseBot (RuFlo V3)

This project is a high-performance algorithmic trading bot designed for Polymarket's crypto micro-markets (5-minute and 15-minute intervals).

## 🚀 Project Overview
- **Purpose**: Automates trading on Polymarket "Up or Down" micro-markets using a streak momentum strategy.
- **Core Technology**: Python, `asyncio`, SQLite, Polymarket (Gamma & CLOB APIs), and Binance Klines API.
- **Architecture**:
    - `src/api/`: Integration with Polymarket and Binance.
    - `src/strategy/`: Strategy logic (Streak detection, Decision, Validation, Position Management).
    - `src/risk/`: Circuit breakers, exposure limits, and emergency kill switch.
    - `src/db/`: SQLite persistence layer.
    - `R_and_D/backtesting/`: Production-grade backtesting engine using Binance historical data.

## 🛠️ Building and Running
- **Environment**: Use the local virtual environment: `source venv_local/bin/activate`.
- **Testing**: Run all unit tests before deployment: `./venv_local/bin/pytest`.
- **Execution**: 
    - Local (Paper Trade): `python src/main.py --env .env.rd --paper`
    - Remote (VPS): Deployment via `rsync` and process management via `pm2`.
- **Configuration**: Managed via `.env` (Live) and `.env.rd` (R&D/Paper).

## 📊 Strategy Optimization (Backtest V3 Insights)
The bot's performance is highly dependent on market regimes and timing. The following optimizations have been identified through 23-day backtesting:

- **Safe Windows (ET Hours)**:
    - Early Morning: 12:00 AM
    - Asian Recovery: 02:00 AM
    - Morning Run (Strongest): 06:00 AM - 08:00 AM, 10:00 AM
    - Lunch Spike: 01:00 PM
    - Evening Reset: 07:00 PM, 09:00 PM
- **Toxic Hours (Avoid)**:
    - 01:00 AM, 03:00 AM - 05:00 AM (Asian Volatility Trap / Dead Zone)
    - 08:00 AM (Pre-US market volatility)
    - 11:00 AM - 01:00 PM (Lunch burn)
    - 03:00 PM - 06:00 PM (US Close chaos / Oracle settlement)
    - 08:00 PM (Late night drift)
    - 11:00 PM (Daily transition)
- **Timeframe Preference**: 15m markets consistently outperform 5m markets in win rate (especially BTC 15m at ~54%).
- **Weekend Logic**: Disable trading during weekends (Saturday - Sunday) to avoid high-toxicity regimes (-6.2% ROI).
- **CRITICAL RULE - NO STOP LOSS**: Never apply a traditional Stop Loss to 5m/15m binary options. The volatility requires surviving deep temporary wicks (up to -97% dips). Cutting losses early (e.g., at -48%) mathematically destroys the strategy because saving a fraction of capital on losers is outweighed by the destruction of full-payout winning trades. Risk management must rely strictly on **Position Sizing** and **Take Profit** logic.

## 📜 Development Conventions
- **Asynchronous**: All I/O and trading loops must use `asyncio`.
- **Logging**: High-signal JSON logging. Use `logger.info("event_name", **payload)` for structured tracking.
- **Verification**: New features must include a corresponding test in the `tests/` directory.
- **Strategic Advice & Constraints**: NEVER propose, recommend, or apply quantitative parameters (e.g., Stop Losses, position sizing adjustments, indicator thresholds) based on raw heuristics or logical assumptions. All strategic advice MUST be mathematically verified against actual PnL simulations first. Furthermore, do not state strategic changes as facts or implement them without explicit user confirmation.
- **Safety First**: Use the `KillSwitch` and `RiskLimits` to prevent catastrophic capital loss.
- **Streak Reset**: Once a trade is taken, the bot must wait for a fresh, non-overlapping streak to form before re-entering (to avoid "Momentum Exhaustion Traps").
