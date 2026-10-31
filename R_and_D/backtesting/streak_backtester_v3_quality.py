#!/usr/bin/env python3
"""
Streak Momentum Backtester — V3.102 (Quality Score Edition)
Evaluates the new Streak Quality Score filter on June 1-2 data.
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
    "intervals": ["1m"], 
    "start_date": "2026-06-01",
    "end_date": "2026-06-02",
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
    "base_entry_price": 0.48, 
    "option_c_coeff": 5.0,     
    "position_size_usdc": 10.0,
    "simulate_guards": True,
    "min_ev_threshold": 0.60,
    "global_cooldown": 60,     
    "max_open_positions": 2,   
    "max_spread_pct_of_ask": 0.60,
    "early_yes_bias_max": 0.55,
    "mid_yes_bias_max": 0.65,
    "late_yes_bias_max": 0.60,
    "min_streak_quality_score": 0.45,
    "streak_quality_window": 7
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
            current_start = int(data[-1][6]) + 1
            if len(data) < 1000:
                break
            time.sleep(0.1)
        except Exception as e:
            print(f"    [!] Error fetching: {e}")
            break
    return klines

def load_data(conn, symbol, start_date, end_date):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    
    cached = get_cached_range(conn, symbol)
    need_fetch = True
    if cached:
        c_min, c_max, count = cached
        if c_min <= start_ms + 60000 and c_max >= end_ms - 60000:
            print(f"  Cache HIT for {symbol} ({count} rows).")
            need_fetch = False
            
    if need_fetch:
        print(f"  Cache MISS for {symbol}. Fetching...")
        klines = fetch_binance_klines(symbol, start_ms - 8 * 3600 * 1000, end_ms + 4 * 3600 * 1000)
        if klines:
            save_klines(conn, symbol, klines)
            
    df = pd.read_sql_query("""
        SELECT open_time, open, high, low, close, volume 
        FROM klines_1m 
        WHERE symbol = ? AND open_time >= ? AND open_time <= ?
        ORDER BY open_time
    """, conn, params=(symbol, start_ms, end_ms))
    
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df

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

def estimate_entry_price(first_minute_row, target_side, config):
    if first_minute_row is None or len(first_minute_row) == 0:
        return config["base_entry_price"]
    open_1m = float(first_minute_row["open"])
    close_1m = float(first_minute_row["close"])
    if open_1m <= 0: return config["base_entry_price"]
    ret_1m = (close_1m - open_1m) / open_1m
    target_sign = 1 if target_side == "YES" else -1
    coeff = config["option_c_coeff"]
    simulated_price = config["base_entry_price"] - (target_sign * coeff * ret_1m)
    return float(np.clip(simulated_price, 0.01, 0.99))

def get_raw_candidates(df_1m, symbol, window_minutes, mode, config):
    df_window = resample_klines(df_1m, window_minutes)
    candidates = []
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    # Start loop from enough history for Quality Score (min 8 outcomes for 7 transitions)
    start_idx = max(8, 3) 
    
    for i in range(start_idx, len(df_window)):
        row = df_window.iloc[i]
        closes_at = row["ts_close"]
        open_at = row["ts_open"]
        
        # Streak detection
        lookback1 = df_window.iloc[i-1]
        lookback2 = df_window.iloc[i-2]
        
        if mode == "streak2":
            if lookback1["outcome"] != lookback2["outcome"]: continue
            streak_dir = lookback1["outcome"]
            streak_str = f"{lookback1['outcome']}-{lookback2['outcome']}"
        elif mode == "streak1":
            streak_dir = lookback1["outcome"]
            streak_str = lookback1["outcome"]
        else: continue
            
        target_side = "YES" if streak_dir == "UP" else "NO"
        
        # Calculate Streak Quality Score
        window_size = config.get("streak_quality_window", 7)
        same_count = 0
        valid_transitions = 0
        for j in range(1, window_size + 1):
            idx_curr = i - j
            idx_prev = i - j - 1
            if idx_prev >= 0:
                o_curr = df_window.iloc[idx_curr]["outcome"]
                o_prev = df_window.iloc[idx_prev]["outcome"]
                valid_transitions += 1
                if o_curr == o_prev:
                    same_count += 1
        streak_quality_score = (same_count / valid_transitions) if valid_transitions >= 5 else None

        # Simulate Entry Price
        open_time_ms = open_at * 1000
        first_min_rows = df_1m[df_1m["open_time"] == open_time_ms]
        first_min_row = first_min_rows.iloc[0] if not first_min_rows.empty else None
        entry_price = estimate_entry_price(first_min_row, target_side, config)
        
        if entry_price < config["min_entry_price"] or entry_price > config["max_entry_price"]:
            continue

        # Simulate Guard Layer
        if config.get("simulate_guards", False):
            ev_ratio = ((1.0 - entry_price) / entry_price) * 0.98
            if ev_ratio < config.get("min_ev_threshold", 0.60): continue
            
            secs_left = (window_minutes * 60) - 60
            yes_price = entry_price if target_side == "YES" else (1.0 - entry_price)
            if secs_left < 180: max_allowed = config.get("late_yes_bias_max", 0.60)
            elif secs_left <= 600: max_allowed = config.get("mid_yes_bias_max", 0.65)
            else: max_allowed = config.get("early_yes_bias_max", 0.55)
            if yes_price >= max_allowed: continue

            simulated_spread = abs(entry_price - config.get("base_entry_price", 0.48)) / config.get("base_entry_price", 0.48)
            if simulated_spread > config.get("max_spread_pct_of_ask", 0.60): continue
            
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
            "pnl": pnl,
            "streak_quality_score": streak_quality_score
        })
    return candidates

def simulate_chrono(raw_candidates, mode, config):
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
        active_positions = [p for p in active_positions if open_at < p["closes_at"]]
        if len(active_positions) >= config.get("max_open_positions", 2): continue
        if config.get("simulate_guards", False):
            if open_at - last_entry_time < config.get("global_cooldown", 60): continue
        last_traded = last_traded_close_ts.get((symbol, window), 0)
        dur_secs = window * 60
        if mode == "streak2":
            lookback2_close = open_at - dur_secs
            if lookback2_close <= last_traded: continue
        elif mode == "streak1":
            lookback1_close = open_at
            if lookback1_close <= last_traded: continue
        if any(p["symbol"] == symbol for p in active_positions): continue
        active_positions.append(c)
        last_traded_close_ts[(symbol, window)] = closes_at
        last_entry_time = open_at
        trade_time_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
        final_trades.append({
            "timestamp_utc": trade_time_utc,
            "symbol": symbol,
            "window": window,
            "streak": c["streak"],
            "side": c["side"],
            "entry_price": c["entry_price"],
            "outcome": c["outcome"],
            "win": c["win"],
            "pnl": c["pnl"],
            "streak_quality_score": c["streak_quality_score"]
        })
    return final_trades

def generate_reports(trades, config):
    if not trades: return "No trades triggered."
    df = pd.DataFrame(trades)
    
    # Baseline
    total_trades = len(df)
    total_win = df["win"].sum()
    win_rate = (total_win / total_trades) * 100
    total_pnl = df["pnl"].sum()
    
    # Quality Filtered
    min_quality = config["min_streak_quality_score"]
    df_filtered = df[df["streak_quality_score"] >= min_quality].copy()
    f_trades = len(df_filtered)
    f_win = df_filtered["win"].sum() if f_trades > 0 else 0
    f_wr = (f_win / f_trades) * 100 if f_trades > 0 else 0
    f_pnl = df_filtered["pnl"].sum() if f_trades > 0 else 0
    
    # Rejections (Trades the filter would have blocked)
    df_rejected = df[df["streak_quality_score"] < min_quality].copy()
    r_trades = len(df_rejected)
    r_win = df_rejected["win"].sum() if r_trades > 0 else 0
    r_wr = (r_win / r_trades) * 100 if r_trades > 0 else 0
    r_pnl = df_rejected["pnl"].sum() if r_trades > 0 else 0

    report = f"""
### 📊 Backtest Summary (June 1-2)
- **Baseline Trades**: {total_trades}
- **Baseline Win Rate**: {win_rate:.2f}%
- **Baseline PnL**: ${total_pnl:+.2f}

### 🟢 Quality Filtered (Score >= {min_quality})
- **Trades Passed**: {f_trades}
- **Win Rate**: {f_wr:.2f}%
- **PnL**: ${f_pnl:+.2f}
- **Trades Saved (Blocked)**: {r_trades}

### 🔴 Filter Rejections (Score < {min_quality})
- **Trades Blocked**: {r_trades}
- **Win Rate of Blocked**: {r_wr:.2f}%
- **PnL of Blocked**: ${r_pnl:+.2f}

**Analysis**: {"✅ Filter is effective (Blocked mostly losers)" if r_wr < win_rate else "⚠️ Filter might be too aggressive (Blocked winners)"}
"""
    
    # Detailed Table
    report += "\n\n#### Detailed Trades\n"
    df["quality_status"] = np.where(df["streak_quality_score"] >= min_quality, "PASS", "REJECT")
    cols = ["timestamp_utc", "symbol", "window", "side", "entry_price", "streak_quality_score", "quality_status", "win", "pnl"]
    df_display = df[cols].copy()
    df_display["timestamp_utc"] = df_display["timestamp_utc"].dt.strftime("%Y-%m-%d %H:%M")
    
    def to_markdown_table(df):
        headers = list(df.columns)
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        for _, row in df.iterrows():
            lines.append("| " + " | ".join(str(row[h]) for h in headers) + " |")
        return "\n".join(lines)
    
    report += to_markdown_table(df_display)
    return report

def main():
    conn = init_db(CONFIG["db_path"])
    all_trades = []
    for symbol in CONFIG["symbols"]:
        df_1m = load_data(conn, symbol, CONFIG["start_date"], CONFIG["end_date"])
        for window in [5, 15]:
            candidates = get_raw_candidates(df_1m, symbol, window, "streak2", CONFIG)
            trades = simulate_chrono(candidates, "streak2", CONFIG)
            all_trades.extend(trades)
    conn.close()
    
    report_md = generate_reports(all_trades, CONFIG)
    report_path = "R_and_D/backtesting/streak_quality_audit_june1_2.md"
    with open(report_path, "w") as f:
        f.write("# Streak Quality Score Backtest Audit (June 1-2)\n")
        f.write(report_md)
    print(f"Results written to {report_path}")

if __name__ == "__main__":
    main()
