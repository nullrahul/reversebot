#!/usr/bin/env python3
"""
Deep Streak Momentum Backtester — Quality Score Edition
Corrects critical flaws from the previous script:
1. Buffers data load so early morning trades aren't skipped.
2. Disables the Asset Cap constraint (per GEMINI.md v3.25).
3. Segments reporting by Timeframe (5m vs 15m) and Safe vs Toxic hours.
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
    "max_open_positions": 3, # Increased to allow concurrent 5m/15m as per v3.25
    "max_spread_pct_of_ask": 0.60,
    "early_yes_bias_max": 0.55,
    "mid_yes_bias_max": 0.65,
    "late_yes_bias_max": 0.60,
    "min_streak_quality_score": 0.45,
    "streak_quality_window": 7
}

TOXIC_HOURS = [1, 3, 4, 8, 11, 12, 15, 16, 17, 20, 22, 23] # ET Hours

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS klines_1m (
            symbol TEXT, open_time INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, open_time))""")
    conn.commit()
    return conn

def load_data_with_buffer(conn, symbol, start_date, end_date):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    
    # BUFFER: Load 12 hours prior so we can calculate 8 outcomes for trades right at 00:00 June 1
    fetch_start_ms = int((start_dt - timedelta(hours=12)).timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
            
    df = pd.read_sql_query("""
        SELECT open_time, open, high, low, close, volume 
        FROM klines_1m 
        WHERE symbol = ? AND open_time >= ? AND open_time <= ?
        ORDER BY open_time
    """, conn, params=(symbol, fetch_start_ms, end_ms))
    
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df

def resample_klines(df_1m, window_minutes):
    rule = f"{window_minutes}min"
    return df_1m.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

def estimate_entry_price(first_minute_row, target_side, config):
    if first_minute_row is None or len(first_minute_row) == 0: return config["base_entry_price"]
    open_1m, close_1m = float(first_minute_row["open"]), float(first_minute_row["close"])
    if open_1m <= 0: return config["base_entry_price"]
    ret_1m = (close_1m - open_1m) / open_1m
    target_sign = 1 if target_side == "YES" else -1
    return float(np.clip(config["base_entry_price"] - (target_sign * config["option_c_coeff"] * ret_1m), 0.01, 0.99))

def get_raw_candidates(df_1m, symbol, window_minutes, config):
    df_window = resample_klines(df_1m, window_minutes)
    candidates = []
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    start_idx = max(8, 3) 
    
    for i in range(start_idx, len(df_window)):
        row = df_window.iloc[i]
        open_at, closes_at = row["ts_open"], row["ts_close"]
        
        # Streak 2 detection
        lookback1, lookback2 = df_window.iloc[i-1], df_window.iloc[i-2]
        if lookback1["outcome"] != lookback2["outcome"]: continue
        streak_dir = lookback1["outcome"]
        target_side = "YES" if streak_dir == "UP" else "NO"
        
        # Quality Score Calculation
        valid_transitions = same_count = 0
        for j in range(1, config["streak_quality_window"] + 1):
            if (i - j - 1) >= 0:
                o_curr, o_prev = df_window.iloc[i - j]["outcome"], df_window.iloc[i - j - 1]["outcome"]
                valid_transitions += 1
                if o_curr == o_prev: same_count += 1
        sq_score = (same_count / valid_transitions) if valid_transitions >= 5 else None

        # Entry Price & Guards
        open_time_ms = open_at * 1000
        first_min_rows = df_1m[df_1m["open_time"] == open_time_ms]
        entry_price = estimate_entry_price(first_min_rows.iloc[0] if not first_min_rows.empty else None, target_side, config)
        
        if not (config["min_entry_price"] <= entry_price <= config["max_entry_price"]): continue

        ev_ratio = ((1.0 - entry_price) / entry_price) * 0.98
        if ev_ratio < config["min_ev_threshold"]: continue
        
        secs_left = dur_secs - 60
        yes_price = entry_price if target_side == "YES" else (1.0 - entry_price)
        max_allowed = config["late_yes_bias_max"] if secs_left < 180 else (config["mid_yes_bias_max"] if secs_left <= 600 else config["early_yes_bias_max"])
        if yes_price >= max_allowed: continue

        simulated_spread = abs(entry_price - config["base_entry_price"]) / config["base_entry_price"]
        if simulated_spread > config["max_spread_pct_of_ask"]: continue
            
        win = (target_side == "YES" and row["outcome"] == "UP") or (target_side == "NO" and row["outcome"] == "DOWN")
        pnl = ((config["position_size_usdc"] / entry_price) * (1.0 if win else 0.0) * 0.98) - config["position_size_usdc"]
        
        candidates.append({
            "open_at": open_at, "closes_at": closes_at, "symbol": symbol, "window": window_minutes,
            "side": target_side, "entry_price": entry_price, "win": win, "pnl": pnl, "sq_score": sq_score
        })
    return candidates

def simulate_chrono(raw_candidates, config):
    raw_candidates.sort(key=lambda x: x["open_at"])
    active_positions, last_traded_close_ts, final_trades = [], {}, []
    target_start_ts = datetime.strptime(config["start_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()

    for c in raw_candidates:
        open_at, symbol, window, closes_at = c["open_at"], c["symbol"], c["window"], c["closes_at"]
        active_positions = [p for p in active_positions if open_at < p["closes_at"]]
        if len(active_positions) >= config["max_open_positions"]: continue
        
        # Disabled asset cap check per v3.25 rules
        
        # Streak Reset Guard
        last_traded = last_traded_close_ts.get((symbol, window), 0)
        if (open_at - (window * 60)) <= last_traded: continue

        active_positions.append(c)
        last_traded_close_ts[(symbol, window)] = closes_at
        
        # Only record trades that happen ON or AFTER the target start date
        if open_at >= target_start_ts:
            trade_dt = datetime.fromtimestamp(open_at, tz=timezone.utc)
            hour_et = trade_dt.astimezone(ZoneInfo("America/New_York")).hour
            c["timestamp_utc"] = trade_dt
            c["hour_et"] = hour_et
            c["is_toxic"] = hour_et in TOXIC_HOURS
            final_trades.append(c)
            
    return final_trades

def generate_deep_report(trades, config):
    df = pd.DataFrame(trades)
    report = ["# 🔬 DEEP AUDIT: Streak Quality Score (June 1-2)\n"]
    min_q = config["min_streak_quality_score"]

    def analyze_segment(df_sub, title):
        if df_sub.empty: return f"### {title}\nNo trades in segment.\n"
        total = len(df_sub)
        pnl = df_sub['pnl'].sum()
        wr = df_sub['win'].mean() * 100
        
        df_pass = df_sub[df_sub['sq_score'] >= min_q]
        pass_pnl = df_pass['pnl'].sum()
        pass_wr = df_pass['win'].mean() * 100 if not df_pass.empty else 0
        
        df_rej = df_sub[df_sub['sq_score'] < min_q]
        rej_pnl = df_rej['pnl'].sum()
        rej_wr = df_rej['win'].mean() * 100 if not df_rej.empty else 0
        
        res = f"### {title}\n"
        res += f"- **Baseline**: {total} trades | {wr:.1f}% WR | **${pnl:+.2f}**\n"
        res += f"- **Filter PASS (>= {min_q})**: {len(df_pass)} trades | {pass_wr:.1f}% WR | **${pass_pnl:+.2f}**\n"
        res += f"- **Filter BLOCKED (< {min_q})**: {len(df_rej)} trades | {rej_wr:.1f}% WR | **${rej_pnl:+.2f}**\n"
        
        if rej_wr < wr:
            res += f"  - ✅ *Good Filter* (Blocked lower-quality trades)\n"
        else:
            res += f"  - ⚠️ *Bad Filter* (Blocked profitable setups)\n"
        return res

    report.append("## 1. Timeframe Segmentation (All Hours)")
    report.append(analyze_segment(df[df['window'] == 5], "5-Minute Markets"))
    report.append(analyze_segment(df[df['window'] == 15], "15-Minute Markets"))
    
    report.append("\n## 2. Market Regime Segmentation (15m Markets Only)")
    df_15 = df[df['window'] == 15]
    report.append(analyze_segment(df_15[~df_15['is_toxic']], "🟢 SAFE HOURS (Optimized Regime)"))
    report.append(analyze_segment(df_15[df_15['is_toxic']], "🔴 TOXIC HOURS"))

    return "\n".join(report)

def main():
    conn = init_db(CONFIG["db_path"])
    raw_candidates = []
    for symbol in CONFIG["symbols"]:
        df_1m = load_data_with_buffer(conn, symbol, CONFIG["start_date"], CONFIG["end_date"])
        for window in [5, 15]:
            raw_candidates.extend(get_raw_candidates(df_1m, symbol, window, CONFIG))
    conn.close()
    
    final_trades = simulate_chrono(raw_candidates, CONFIG)
    report_str = generate_deep_report(final_trades, CONFIG)
    
    out_path = "R_and_D/backtesting/streak_quality_deep_audit.md"
    with open(out_path, "w") as f: f.write(report_str)
    print(f"Deep audit written to {out_path}")

if __name__ == "__main__":
    main()
