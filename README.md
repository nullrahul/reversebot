# ⚡ Polymarket ReverseBot (RuFlo V3)

A high-performance algorithmic trading bot designed for Polymarket high-frequency crypto micro-markets (5m/15m intervals).

---

## 🎯 Current Active Strategy: Streak Momentum (v0.1.0)
The bot monitors 5-minute and 15-minute BTC and ETH micro-markets (`[coin]-updown-[5m/15m]-[timestamp]`). It executes trades based on verified consecutive candle streaks:
* **Bull Trend (`UP-UP`)**: Buys the **YES** token (UP outcome).
* **Bear Trend (`DOWN-DOWN`)**: Buys the **NO** token (DOWN outcome).
* **Mixed / Flat**: No entry.

### Core Safeguards:
1. **Dynamic Resolution Fallback**: Polling the Polymarket Gamma API for the immediate past candles ($T_1$ and $T_2$). If Gamma lacks outcomes, it falls back keylessly to Binance Klines API to resolve the candles instantly.
2. **Future Guard**: Skips generation/polling for future markets that haven't started yet.
3. **Rejection Rate Limiting**: In-memory logger prevents redundant rejection writes to SQLite database, saving gigabytes of VPS storage.
4. **Aggressive Exits**: Progressive slippage exit (progressive slippage: 1%, 3%, 5%) for Stop Losses to prevent zombie orders in fast-moving books.

---

## 📁 Clean Codebase Directory Map

```bash
├── src/                        # Core bot production source code
│   ├── api/                    # Integrations (Gamma API, CLOB Client, WebSockets, Dashboard, TG)
│   │   ├── clob_client.py      # Patched HTTP/2 connection client for high speed
│   │   ├── dashboard.py        # Dashboard API endpoints & PnL math
│   │   └── telegram_client.py  # Telegram controls & notifications
│   ├── db/                     # Data access & Persistence Layer
│   │   ├── queries.py          # SQL transaction queries (upserts, stats)
│   │   ├── schema.sql          # SQLite schema specifications
│   │   └── rejections.py       # In-memory rejection rate-limiting logger
│   ├── strategy/               # Quantitative Strategy & Entry/Exit Execution
│   │   ├── scanner.py          # Ephemeral micro-market discovery & trend streak checks
│   │   ├── decision.py         # Signal auditing, side selection, and order routing
│   │   ├── validator.py        # Spreads, volumes, categories, and time bounds validation
│   │   └── position_mgr.py     # Split-TP1/TP2 scaling engine & aggressive SL closing
│   ├── risk/                   # Portfolio Risk & Circuit Breakers
│   │   ├── limits.py           # Daily loss caps and exposure ceilings
│   │   └── kill_switch.py      # Emergency shutdown & position liquidator
│   └── main.py                 # Core event loop engine & initialization orchestrator
│
├── tests/                      # Extensive test coverage
│   ├── test_auth.py            # API headers, nonces, timestamps, signature tests
│   ├── test_db.py              # SQLite integrity, WAL modes, schema, query validations
│   ├── test_kill_switch.py     # Emergency cancelation & multi-exception trackers
│   ├── test_position_mgr.py    # Order execution, locks, integer rounding, and FOK rules
│   └── test_validator.py       # Spreads, price stability, and volume validations
│
├── config/                     # Static environment profiles
├── data/                       # Production Database storage (reversebot_rd.db, reversebot.db)
├── dashboard/                  # Dashboard frontend assets (HTML, CSS, JS)
├── logs_rd/                    # Diagnostic run logs directory
├── .env.rd                     # R&D profile (Paper Trade on broad micro-markets)
├── .env                        # Live production environment profile
├── CLAUDE.md                   # Command quick references
└── GEMINI.md                   # Active status, R&D logs, and history
```

---

## 🛠️ CLI Quick Reference

### 1. Local Virtual Environment Setup
Ensure you are using the local Macbook virtual environment to execute command sequences:
```bash
source venv_local/bin/activate
```

### 2. Running Unit Tests
All 56 unit tests must pass before any synchronization/deployment:
```bash
./venv_local/bin/pytest
```

### 3. Running the Bot Locally
Run in R&D (Paper-Trading) mode using the `.env.rd` profile:
```bash
python src/main.py --env .env.rd
```

### 4. Remote VPS Deployment
The VPS production files are deployed under `~/reversebot/`. Synchronize local changes to the remote Ubuntu VPS `68.210.96.175` via `rsync` (excluding data/databases, virtual environments, and secrets):
```bash
rsync -avz --exclude="data/" --exclude="logs_rd/" --exclude="logs/" --exclude=".env*" --exclude="venv*/" --exclude="node_modules/" --exclude="*.db*" --exclude=".git/" ./ reversebot-vps:~/reversebot/
```

### 5. VPS PM2 Execution
Manage the active R&D bot process on the VPS using PM2:
```bash
# Monitor logs
pm2 logs reversebot_rd

# Restart process
pm2 restart reversebot_rd

# View active instances
pm2 status
```