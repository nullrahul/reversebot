#!/usr/bin/env python3
"""
Streak Momentum Backtester — V3 (Production-Grade)
Faithfully replicates the live Reversebot trading pipeline.
Uses local SQLite database as a cache to prevent Binance API spam.
"""

import os
import time
import sqlite3
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# --- Configuration ---
CONFIG = {
    "db_path": os.path.join(os.path.dirname(__file__), "backtest_cache.db"),
    "symbols": ["BTC", "ETH"],
    "intervals": ["1m"], # We fetch 1m klines and resample to 5m/15m
    "start_date": "2026-05-01",
    "end_date": "2026-05-23",
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
    "base_entry_price": 0.48, # Live bot median entry price
    "option_c_coeff": 5.0,     # Option C multiplier for first minute return
    "position_size_usdc": 10.0,
    # --- E4 & E5 Guard Simulation & Concurrency Limits ---
    "simulate_guards": True,
    "min_ev_threshold": 0.60,
    "global_cooldown": 60,     # 60s cooldown between entries
    "max_open_positions": 2,   # Enforce global max open positions limit
    "max_spread_pct_of_ask": 0.60,
    "early_yes_bias_max": 0.55,
    "mid_yes_bias_max": 0.65,
    "late_yes_bias_max": 0.60,
}

# --- Database & Cache Setup ---
def init_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS klines_1m (
            symbol TEXT,
            open_time INTEGER,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (symbol, open_time)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_klines_symbol_time ON klines_1m (symbol, open_time)")
    conn.commit()
    return conn

def get_cached_range(conn, symbol):
    c = conn.cursor()
    c.execute("SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines_1m WHERE symbol = ?", (symbol,))
    row = c.fetchone()
    if row and row[0] is not None:
        return row[0], row[1], row[2]
    return None

def save_klines(conn, symbol, klines):
    c = conn.cursor()
    data = []
    for k in klines:
        # k format: [open_time, open, high, low, close, volume, close_time, ...]
        data.append((
            symbol,
            int(k[0]),
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
            float(k[5])
        ))
    c.executemany("""
        INSERT OR REPLACE INTO klines_1m (symbol, open_time, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, data)
    conn.commit()

# --- Binance Kline Fetcher ---
def fetch_binance_klines(symbol, start_ms, end_ms):
    binance_symbol = f"{symbol}USDT"
    url = "https://api.binance.com/api/v3/klines"
    klines = []
    current_start = start_ms
    
    print(f"  Fetching {symbol} from Binance API...")
    while current_start < end_ms:
        params = {
            "symbol": binance_symbol,
            "interval": "1m",
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            klines.extend(data)
            # Next start is the close time of last candle + 1ms
            current_start = int(data[-1][6]) + 1
            print(f"    Progress: fetched {len(klines)} candles, up to {datetime.fromtimestamp(current_start/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            if len(data) < 1000:
                break
            time.sleep(0.1) # Respect rate limits
        except Exception as e:
            print(f"    [!] Error fetching: {e}")
            break
            
    return klines

def load_data(conn, symbol, start_date, end_date):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    
    # Check cache
    cached = get_cached_range(conn, symbol)
    need_fetch = True
    if cached:
        c_min, c_max, count = cached
        # If cache covers our requested range (with small tolerance)
        if c_min <= start_ms + 60000 and c_max >= end_ms - 60000:
            print(f"  Cache HIT for {symbol} ({count} rows). No API call needed.")
            need_fetch = False
            
    if need_fetch:
        print(f"  Cache MISS for {symbol}. Fetching range {start_date} to {end_date}...")
        # To be safe, fetch a bit extra on the sides
        fetch_start = start_ms - 4 * 3600 * 1000 # 4 hours before
        fetch_end = end_ms + 4 * 3600 * 1000    # 4 hours after
        klines = fetch_binance_klines(symbol, fetch_start, fetch_end)
        if klines:
            save_klines(conn, symbol, klines)
            print(f"  Successfully cached {len(klines)} rows for {symbol}.")
            
    # Read from DB
    df = pd.read_sql_query("""
        SELECT open_time, open, high, low, close, volume 
        FROM klines_1m 
        WHERE symbol = ? AND open_time >= ? AND open_time <= ?
        ORDER BY open_time
    """, conn, params=(symbol, start_ms, end_ms))
    
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df

# --- Resampling Helper ---
def resample_klines(df_1m, window_minutes):
    rule = f"{window_minutes}min"
    resampler = df_1m.resample(rule)
    df_agg = resampler.agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()
    return df_agg

# --- Entry Price Simulation (Option C) ---
def estimate_entry_price(first_minute_row, target_side, config):
    """
    Implements Option C:
    If early candle confirms streak direction, entry is cheaper (lower price);
    if contradicts, entry is more expensive (higher price).
    """
    if first_minute_row is None or len(first_minute_row) == 0:
        return config["base_entry_price"]
        
    open_1m = float(first_minute_row["open"])
    close_1m = float(first_minute_row["close"])
    
    if open_1m <= 0:
        return config["base_entry_price"]
        
    ret_1m = (close_1m - open_1m) / open_1m
    
    # Target direction: YES is UP (+1), NO is DOWN (-1)
    target_sign = 1 if target_side == "YES" else -1
    
    # Under user's selection:
    # If confirmed (e.g. YES target and positive ret_1m, or NO target and negative ret_1m):
    # entry is cheaper (lower price) -> subtract from base
    # If contradicts (e.g. YES target and negative ret_1m, or NO target and positive ret_1m):
    # entry is more expensive (higher price) -> add to base
    coeff = config["option_c_coeff"]
    simulated_price = config["base_entry_price"] - (target_sign * coeff * ret_1m)
    
    # Keep it within sane bounds [0.01, 0.99]
    return float(np.clip(simulated_price, 0.01, 0.99))

# --- Backtest Engine ---
def get_raw_candidates(df_1m, symbol, window_minutes, mode, config):
    df_window = resample_klines(df_1m, window_minutes)
    
    candidates = []
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    for i in range(3, len(df_window)):
        row = df_window.iloc[i]
        closes_at = row["ts_close"]
        open_at = row["ts_open"]
        
        # Streak detection
        lookback1 = df_window.iloc[i-1]
        lookback2 = df_window.iloc[i-2]
        
        if mode == "streak2":
            if lookback1["outcome"] != lookback2["outcome"]:
                continue
            streak_dir = lookback1["outcome"]
            streak_str = f"{lookback1['outcome']}-{lookback2['outcome']}"
        elif mode == "streak1":
            streak_dir = lookback1["outcome"]
            streak_str = lookback1["outcome"]
        else:
            continue
            
        target_side = "YES" if streak_dir == "UP" else "NO"
        
        # Simulate Entry Price
        open_time_ms = open_at * 1000
        first_min_rows = df_1m[df_1m["open_time"] == open_time_ms]
        first_min_row = first_min_rows.iloc[0] if not first_min_rows.empty else None
        entry_price = estimate_entry_price(first_min_row, target_side, config)
        
        # Filter entry price
        if entry_price < config["min_entry_price"] or entry_price > config["max_entry_price"]:
            continue

        # Simulate Guard Layer (E4)
        if config.get("simulate_guards", False):
            # 1. EV Guard
            # EV ratio = ((1.0 - entry_price) / entry_price) * 0.98
            ev_ratio = ((1.0 - entry_price) / entry_price) * 0.98
            if ev_ratio < config.get("min_ev_threshold", 0.60):
                continue
                
            # 2. Dynamic Yes Bias Guard
            secs_left = (window_minutes * 60) - 60
            yes_price = entry_price if target_side == "YES" else (1.0 - entry_price)
            
            if secs_left < 180:
                max_allowed = config.get("late_yes_bias_max", 0.60)
            elif secs_left <= 600:
                max_allowed = config.get("mid_yes_bias_max", 0.65)
            else:
                max_allowed = config.get("early_yes_bias_max", 0.55)
                
            if yes_price >= max_allowed:
                continue

            # 3. Dynamic Spread Guard
            simulated_spread = abs(entry_price - config.get("base_entry_price", 0.48)) / config.get("base_entry_price", 0.48)
            if simulated_spread > config.get("max_spread_pct_of_ask", 0.60):
                continue
            
        # Resolution
        actual_outcome = row["outcome"]
        win = (target_side == "YES" and actual_outcome == "UP") or (target_side == "NO" and actual_outcome == "DOWN")
        payout = 1.0 if win else 0.0
        
        shares = config["position_size_usdc"] / entry_price
        pnl = (shares * payout * 0.98) - config["position_size_usdc"]
        
        candidates.append({
            "open_at": open_at,
            "closes_at": closes_at,
            "symbol": symbol,
            "window": window_minutes,
            "streak": streak_str,
            "side": target_side,
            "entry_price": entry_price,
            "outcome": actual_outcome,
            "win": win,
            "pnl": pnl
        })
        
    return candidates

def simulate_chrono(raw_candidates, mode, config):
    # Sort chronologically by open_at
    raw_candidates.sort(key=lambda x: x["open_at"])
    
    active_positions = []
    last_traded_close_ts = {}
    final_trades = []
    last_entry_time = 0
    
    for c in raw_candidates:
        open_at = c["open_at"]
        symbol = c["symbol"]
        window = c["window"]
        closes_at = c["closes_at"]
        
        # 1. Remove resolved positions
        active_positions = [p for p in active_positions if open_at < p["closes_at"]]
        
        # Check Global Concurrency limit (E5)
        if len(active_positions) >= config.get("max_open_positions", 2):
            continue

        # Check Global Cooldown (E4)
        if config.get("simulate_guards", False):
            if open_at - last_entry_time < config.get("global_cooldown", 60):
                continue

        # 2. Check Streak Reset Guard
        last_traded = last_traded_close_ts.get((symbol, window), 0)
        dur_secs = window * 60
        
        if mode == "streak2":
            lookback2_close = open_at - dur_secs
            if lookback2_close <= last_traded:
                continue
        elif mode == "streak1":
            lookback1_close = open_at
            if lookback1_close <= last_traded:
                continue
                
        # 3. Check Asset Cap (1 open position per symbol)
        asset_overlap = any(p["symbol"] == symbol for p in active_positions)
        if asset_overlap:
            continue
            
        # 4. Execute!
        active_positions.append(c)
        last_traded_close_ts[(symbol, window)] = closes_at
        last_entry_time = open_at
        
        # Construct trade dict for reporting
        trade_time_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
        trade_time_et = trade_time_utc.astimezone(ZoneInfo("America/New_York"))
        
        final_trades.append({
            "timestamp_utc": trade_time_utc,
            "timestamp_et": trade_time_et,
            "symbol": symbol,
            "window": window,
            "streak": c["streak"],
            "side": c["side"],
            "entry_price": c["entry_price"],
            "outcome": c["outcome"],
            "win": c["win"],
            "pnl": c["pnl"],
            "day_of_week": trade_time_et.strftime("%A"),
            "is_weekend": trade_time_et.weekday() in (5, 6),
            "hour_et": trade_time_et.hour
        })
        
    return final_trades

def to_markdown_table(df):
    if df.empty:
        return ""
    headers = list(df.columns)
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |"
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[h]) for h in headers) + " |")
    return "\n".join(lines)

# --- Stats Generation ---
def generate_reports(all_trades_streak2, all_trades_streak1):
    reports = []
    
    # Define toxic hours to filter out (ET)
    TOXIC_HOURS = {
        "streak2": [1, 3, 4, 8, 11, 12, 15, 16, 17, 20, 22, 23],
        "streak1": [0, 1, 4, 5, 8, 12, 14, 17, 20, 21, 23]
    }
    
    for mode, trades in [("Streak-2 (Current Live)", all_trades_streak2), ("Streak-1 (1-Candle Momentum)", all_trades_streak1)]:
        if not trades:
            reports.append(f"### {mode}\nNo trades triggered.")
            continue
            
        df = pd.DataFrame(trades)
        
        # 1. Baseline Performance
        total_trades = len(df)
        total_win = df["win"].sum()
        win_rate = (total_win / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = df["pnl"].sum()
        total_size = len(df) * CONFIG["position_size_usdc"]
        roi = (total_pnl / total_size) * 100 if total_size > 0 else 0
        
        # 2. Optimized Performance (Skip Weekends + Skip Toxic Hours)
        key_type = "streak2" if "Streak-2" in mode else "streak1"
        df_opt = df[(~df["is_weekend"]) & (~df["hour_et"].isin(TOXIC_HOURS[key_type]))].copy()
        
        opt_trades = len(df_opt)
        opt_win = df_opt["win"].sum() if opt_trades > 0 else 0
        opt_wr = (opt_win / opt_trades) * 100 if opt_trades > 0 else 0
        opt_pnl = df_opt["pnl"].sum() if opt_trades > 0 else 0
        opt_size = opt_trades * CONFIG["position_size_usdc"]
        opt_roi = (opt_pnl / opt_size) * 100 if opt_size > 0 else 0
        
        # 3. Create Comparison Table
        comparison_data = [
            {"Regime": "🔴 **Baseline (No Filters)**", "Trades": total_trades, "Win Rate": f"{win_rate:.1f}%", "PnL": f"${total_pnl:+.2f}", "ROI": f"{roi:+.1f}%"},
            {"Regime": "🟢 **Optimized (No Weekends + Safe Hours)**", "Trades": opt_trades, "Win Rate": f"{opt_wr:.1f}%", "PnL": f"${opt_pnl:+.2f}", "ROI": f"{opt_roi:.1f}%"}
        ]
        df_comparison = pd.DataFrame(comparison_data)
        
        # Breakdown by Asset x Window (Optimized Regime)
        breakdown = []
        if not df_opt.empty:
            for (sym, win_size), group in df_opt.groupby(["symbol", "window"]):
                g_trades = len(group)
                g_wins = group["win"].sum()
                g_wr = (g_wins / g_trades) * 100
                g_pnl = group["pnl"].sum()
                g_roi = (g_pnl / (g_trades * CONFIG["position_size_usdc"])) * 100
                breakdown.append({
                    "Asset": f"{sym} {win_size}m",
                    "Trades": g_trades,
                    "Win Rate": f"{g_wr:.1f}%",
                    "PnL": f"${g_pnl:+.2f}",
                    "ROI": f"{g_roi:+.1f}%"
                })
        df_breakdown = pd.DataFrame(breakdown)
        
        # Weekend vs Weekday (Baseline)
        day_split = []
        for is_we, group in df.groupby("is_weekend"):
            g_trades = len(group)
            g_wins = group["win"].sum()
            g_wr = (g_wins / g_trades) * 100
            g_pnl = group["pnl"].sum()
            g_roi = (g_pnl / (g_trades * CONFIG["position_size_usdc"])) * 100
            label = "Weekend (Sat-Sun)" if is_we else "Weekday (Mon-Fri)"
            day_split.append({
                "Regime": label,
                "Trades": g_trades,
                "Win Rate": f"{g_wr:.1f}%",
                "PnL": f"${g_pnl:+.2f}",
                "ROI": f"{g_roi:+.1f}%"
            })
        df_day_split = pd.DataFrame(day_split)
        
        # Entry Price Bucketing (Baseline)
        df["price_bucket"] = pd.cut(df["entry_price"], bins=[0.45, 0.499, 0.549, 0.599, 0.65], labels=["$0.45-$0.49", "$0.50-$0.54", "$0.55-$0.59", "$0.60-$0.65"])
        bucket_stats = []
        for bucket, group in df.groupby("price_bucket", observed=False):
            if len(group) == 0:
                continue
            g_trades = len(group)
            g_wins = group["win"].sum()
            g_wr = (g_wins / g_trades) * 100
            g_pnl = group["pnl"].sum()
            bucket_stats.append({
                "Bucket": bucket,
                "Trades": g_trades,
                "Win Rate": f"{g_wr:.1f}%",
                "PnL": f"${g_pnl:+.2f}"
            })
        df_bucket = pd.DataFrame(bucket_stats)
        
        # Hourly performance (ET, Baseline)
        hourly_stats = []
        for hour, group in df.groupby("hour_et"):
            g_trades = len(group)
            g_wins = group["win"].sum()
            g_wr = (g_wins / g_trades) * 100
            g_pnl = group["pnl"].sum()
            hourly_stats.append({
                "Hour (ET)": f"{hour:02d}:00",
                "Trades": g_trades,
                "Win Rate": f"{g_wr:.1f}%",
                "PnL": f"${g_pnl:+.2f}"
            })
        df_hourly = pd.DataFrame(hourly_stats).sort_values("Hour (ET)")
        
        # Format report markdown
        report_md = f"""
### 📈 {mode} Results

#### 📊 Baseline vs. Optimized Comparison
*Optimized regime excludes all weekends and high-toxicity session hours: ET Hours {TOXIC_HOURS[key_type]}*

{to_markdown_table(df_comparison)}

#### 🟢 Optimized Segment Breakdown (Filter Active)
{to_markdown_table(df_breakdown) if not df_breakdown.empty else "No trades triggered in optimized regime."}

#### 🔴 Baseline: Weekend vs Weekday Split
{to_markdown_table(df_day_split)}

#### 🔴 Baseline: Entry Price Bucketing
{to_markdown_table(df_bucket)}

#### 🔴 Baseline: Hourly Performance (ET)
<details>
<summary>View Hourly Performance</summary>

{to_markdown_table(df_hourly)}

</details>
"""
        reports.append(report_md)
        
    return "\n\n".join(reports)

# --- Main Exec ---
def main():
    print("=" * 80)
    print("STREAK MOMENTUM BACKTESTER — V3")
    print(f"Date Range: {CONFIG['start_date']} to {CONFIG['end_date']}")
    print("=" * 80)
    
    # Create output dir if needed
    os.makedirs(os.path.dirname(CONFIG["db_path"]), exist_ok=True)
    
    conn = init_db(CONFIG["db_path"])
    
    data_cache = {}
    for symbol in CONFIG["symbols"]:
        print(f"Loading data for {symbol}...")
        df_1m = load_data(conn, symbol, CONFIG["start_date"], CONFIG["end_date"])
        data_cache[symbol] = df_1m
        print(f"Loaded {len(df_1m)} rows.")
        
    conn.close()
    
    print("\nRunning backtests...")
    raw_candidates_streak2 = []
    raw_candidates_streak1 = []
    
    for symbol in CONFIG["symbols"]:
        df_1m = data_cache[symbol]
        for window in [5, 15]:
            print(f"  → Generating candidates for {symbol} {window}m...")
            # Generate Streak-2 candidates
            c2 = get_raw_candidates(df_1m, symbol, window, "streak2", CONFIG)
            raw_candidates_streak2.extend(c2)
            # Generate Streak-1 candidates
            c1 = get_raw_candidates(df_1m, symbol, window, "streak1", CONFIG)
            raw_candidates_streak1.extend(c1)
            
    print("\nSimulating chronological execution with Asset Cap and Streak Reset Guard...")
    all_trades_streak2 = simulate_chrono(raw_candidates_streak2, "streak2", CONFIG)
    all_trades_streak1 = simulate_chrono(raw_candidates_streak1, "streak1", CONFIG)
    
    print(f"\nBacktest complete.")
    print(f"Streak-2 trades: {len(all_trades_streak2)}")
    print(f"Streak-1 trades: {len(all_trades_streak1)}")
    
    # Generate reports
    report_md = generate_reports(all_trades_streak2, all_trades_streak1)
    
    # Write to local file
    report_path = os.path.join(os.path.dirname(__file__), "backtest_results_v3.md")
    with open(report_path, "w") as f:
        f.write(f"# Streak Momentum Backtesting Report\n")
        f.write(f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"### Parameters:\n")
        f.write(f"- **Start Date**: {CONFIG['start_date']}\n")
        f.write(f"- **End Date**: {CONFIG['end_date']}\n")
        f.write(f"- **Symbols**: {', '.join(CONFIG['symbols'])}\n")
        f.write(f"- **Entry Price Filter**: {CONFIG['min_entry_price']} to {CONFIG['max_entry_price']}\n")
        f.write(f"- **Entry Method**: Option C (Binance first minute return estimation, coeff={CONFIG['option_c_coeff']})\n\n")
        f.write(report_md)
        
    print(f"\nResults written to: {report_path}")

if __name__ == "__main__":
    main()
