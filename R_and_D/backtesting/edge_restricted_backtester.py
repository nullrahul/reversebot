#!/usr/bin/env python3
"""
Edge Restricted Backtester & Grid Search Optimization
Analyzes historical signal data and segments by:
1. Asset (BTC, ETH)
2. Window size (5m, 15m)
3. Contract Side (YES, NO)
4. Temporal Band (Weekday vs Weekend)
5. Streak Depth (2, 3, 4, 5+)
6. Entry Price Brackets ([0.30-0.40], [0.40-0.45], [0.45-0.50], [0.50-0.55], [0.55-0.60], [0.60-0.70])

Computes Win Rate, PnL, ROI, and Expected Value (EV) to pinpoint profitable configurations.
"""

import os
import sqlite3
import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Config mapping the original backtester parameters
CONFIG = {
    "db_path": os.path.join(os.path.dirname(__file__), "backtest_cache.db"),
    "symbols": ["BTC", "ETH"],
    "start_date": "2026-05-13",
    "end_date": "2026-06-13",
    "position_size_usdc": 10.0,
    "spread": 0.02,            # 2.0 cents bid-ask spread on entry
    "fee_multiplier": 0.98,     # 2% transaction fee on net winnings
    "default_vol": {
        "BTC": 0.0007,
        "ETH": 0.0009
    }
}

# --- Normal CDF Helper ---
def std_normal_cdf(z):
    try:
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    except Exception:
        return 0.5

# --- Database Loader ---
def load_data(db_path, symbol, start_date, end_date):
    conn = sqlite3.connect(db_path)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    
    df = pd.read_sql_query("""
        SELECT open_time, open, high, low, close, volume 
        FROM klines_1m 
        WHERE symbol = ? AND open_time >= ? AND open_time <= ?
        ORDER BY open_time
    """, conn, params=(symbol, start_ms, end_ms))
    conn.close()
    
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("timestamp", inplace=True)
    
    # Calculate rolling 1-minute return and standard deviation over 30 minutes
    df["return"] = (df["close"] - df["open"]) / df["open"]
    df["vol_30m"] = df["return"].rolling(30).std()
    
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

# --- Option Pricing Model ---
def estimate_contract_price(s_0, s_t, t, window_minutes, rolling_vol_1m, target_side):
    time_left = window_minutes - t
    if time_left <= 0:
        p_yes = 1.0 if s_t > s_0 else 0.0
    else:
        std_dev = rolling_vol_1m * math.sqrt(time_left)
        if std_dev <= 0:
            p_yes = 1.0 if s_t > s_0 else 0.0
        else:
            ret_from_strike = (s_t - s_0) / s_0
            z = ret_from_strike / std_dev
            p_yes = std_normal_cdf(z)
            
    p_target = p_yes if target_side == "YES" else (1.0 - p_yes)
    return float(np.clip(p_target, 0.01, 0.99))

# --- Signal & Trade Generation ---
def generate_all_trades(df_1m, symbol, window_minutes, config):
    df_window = resample_klines(df_1m, window_minutes)
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    trades = []
    
    for i in range(30, len(df_window) - 1):
        row = df_window.iloc[i]
        open_at = row["ts_open"]
        closes_at = row["ts_close"]
        s_0 = row["open"]
        
        # Volatility
        start_ms = open_at * 1000
        start_rows = df_1m[df_1m["open_time"] == start_ms]
        if start_rows.empty:
            continue
        start_row = start_rows.iloc[0]
        vol_30m = start_row.get("vol_30m")
        if pd.isna(vol_30m) or vol_30m <= 0:
            vol_30m = config["default_vol"][symbol]
            
        # First minute
        first_min_rows = df_1m[df_1m["open_time"] == start_ms]
        if first_min_rows.empty:
            continue
        first_min_row = first_min_rows.iloc[0]
        s_1 = first_min_row["close"]
        
        # Streak
        lookback1 = df_window.iloc[i-1]
        lookback2 = df_window.iloc[i-2]
        lookback3 = df_window.iloc[i-3]
        
        streak_depth = 1
        streak_dir = lookback1["outcome"]
        if lookback2["outcome"] == streak_dir:
            streak_depth = 2
            if lookback3["outcome"] == streak_dir:
                streak_depth = 3
                # Let's count further streak depth if needed
                idx = 4
                while i - idx >= 0 and df_window.iloc[i-idx]["outcome"] == streak_dir:
                    streak_depth += 1
                    idx += 1
        
        # Day of week in EST
        dt_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
        dt_est = dt_utc.astimezone(ZoneInfo("America/New_York"))
        day_type = "WEEKEND" if dt_est.weekday() in (5, 6) else "WEEKDAY"
        
        # Final outcome
        final_close = row["close"]
        actual_outcome = "UP" if final_close > s_0 else "DOWN"
        
        # Simulate both YES and NO entries to evaluate their respective edges
        for side in ["YES", "NO"]:
            # Evaluate price at t=1
            p_1 = estimate_contract_price(s_0, s_1, 1, window_minutes, vol_30m, side)
            entry_price = float(np.clip(p_1 + config["spread"], 0.01, 0.99))
            
            # Outcome calculation
            win = (side == "YES" and actual_outcome == "UP") or (side == "NO" and actual_outcome == "DOWN")
            
            # Position sizing
            raw_shares = config["position_size_usdc"] / entry_price
            shares = int(raw_shares)
            if shares < 5:
                shares = 5
            cost = shares * entry_price
            
            # PnL with fee on net winnings
            if win:
                pnl = (shares * 1.0 * config["fee_multiplier"]) - cost
            else:
                pnl = -cost
                
            # Price Bracket
            if entry_price < 0.40:
                price_bracket = "[0.30-0.40)"
            elif entry_price < 0.45:
                price_bracket = "[0.40-0.45)"
            elif entry_price < 0.50:
                price_bracket = "[0.45-0.50)"
            elif entry_price < 0.55:
                price_bracket = "[0.50-0.55)"
            elif entry_price < 0.60:
                price_bracket = "[0.55-0.60)"
            elif entry_price <= 0.70:
                price_bracket = "[0.60-0.70]"
            else:
                price_bracket = "Outside Range (>0.70)"
                
            # Group streak depth
            streak_depth_group = str(streak_depth) if streak_depth < 5 else "5+"
            
            trades.append({
                "symbol": symbol,
                "window": window_minutes,
                "side": side,
                "day_type": day_type,
                "streak_depth": streak_depth_group,
                "price_bracket": price_bracket,
                "entry_price": entry_price,
                "win": win,
                "cost": cost,
                "pnl": pnl
            })
            
    return trades

def print_summary(df, title, groupby_cols):
    print(f"\n=== {title} ===")
    grouped = df.groupby(groupby_cols).agg(
        trades=("win", "count"),
        wins=("win", "sum"),
        total_pnl=("pnl", "sum"),
        total_cost=("cost", "sum")
    ).reset_index()
    
    grouped["win_rate"] = (grouped["wins"] / grouped["trades"]) * 100
    grouped["roi"] = (grouped["total_pnl"] / grouped["total_cost"]) * 100
    grouped["ev"] = grouped["total_pnl"] / grouped["trades"]
    
    # Sort by EV descending
    grouped = grouped.sort_values(by="ev", ascending=False)
    
    # Format floats
    grouped["win_rate"] = grouped["win_rate"].map("{:.1f}%".format)
    grouped["roi"] = grouped["roi"].map("{:+.1f}%".format)
    grouped["ev"] = grouped["ev"].map("{:+.4f}".format)
    grouped["total_pnl"] = grouped["total_pnl"].map("{:+.2f}".format)
    
    print(grouped.to_string(index=False))

def main():
    print("Loading data and generating signals...")
    all_trades = []
    for symbol in CONFIG["symbols"]:
        df_1m = load_data(CONFIG["db_path"], symbol, CONFIG["start_date"], CONFIG["end_date"])
        for window in [5, 15]:
            trades = generate_all_trades(df_1m, symbol, window, CONFIG)
            all_trades.extend(trades)
            
    df = pd.DataFrame(all_trades)
    print(f"Generated {len(df)} total potential contract trades.")
    
    # Filter out entries outside our standard price range (0.30 - 0.70)
    df_valid = df[df["price_bracket"] != "Outside Range (>0.70)"]
    
    # 1. High-level Side performance (YES vs NO)
    print_summary(df_valid, "Performance by Contract Side", ["side"])
    
    # 2. Performance by Day Type
    print_summary(df_valid, "Performance by Day Type", ["day_type"])
    
    # 3. Performance by Price Bracket
    print_summary(df_valid, "Performance by Price Bracket", ["price_bracket"])
    
    # 4. Performance by Streak Depth
    print_summary(df_valid, "Performance by Streak Depth (including streak 1)", ["streak_depth"])
    
    # 5. Multi-dimensional Grid Search (Minimum 15 trades for statistical relevance)
    print("\nRunning Grid Search for Profitable Pockets (Min 15 trades)...")
    grouped_grid = df_valid.groupby(["symbol", "window", "side", "day_type", "streak_depth", "price_bracket"]).agg(
        trades=("win", "count"),
        wins=("win", "sum"),
        total_pnl=("pnl", "sum"),
        total_cost=("cost", "sum")
    ).reset_index()
    
    grouped_grid = grouped_grid[grouped_grid["trades"] >= 15]
    grouped_grid["win_rate"] = (grouped_grid["wins"] / grouped_grid["trades"]) * 100
    grouped_grid["roi"] = (grouped_grid["total_pnl"] / grouped_grid["total_cost"]) * 100
    grouped_grid["ev"] = grouped_grid["total_pnl"] / grouped_grid["trades"]
    
    # Sort by EV descending
    profitable_grid = grouped_grid[grouped_grid["ev"] > 0].sort_values(by="ev", ascending=False)
    
    # Format floats
    profitable_grid["win_rate"] = profitable_grid["win_rate"].map("{:.1f}%".format)
    profitable_grid["roi"] = profitable_grid["roi"].map("{:+.1f}%".format)
    profitable_grid["ev"] = profitable_grid["ev"].map("{:+.4f}".format)
    profitable_grid["total_pnl"] = profitable_grid["total_pnl"].map("{:+.2f}".format)
    profitable_grid["total_cost"] = profitable_grid["total_cost"].map("{:.2f}".format)
    
    print(f"\nDiscovered {len(profitable_grid)} profitable pockets out of {len(grouped_grid)} tested configurations:")
    print(profitable_grid.to_string(index=False))

if __name__ == "__main__":
    main()
