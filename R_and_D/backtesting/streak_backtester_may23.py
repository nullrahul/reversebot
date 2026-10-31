#!/usr/bin/env python3
"""
Streak Momentum Backtest - May 23, 2026 Mimic Audit
Compares backtester v3 against actual paper trades executed on May 23, 2026.
"""

import os
import sys
import time
import sqlite3
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

# --- Configuration (matching VPS runtime on May 23) ---
CONFIG = {
    "db_path": os.path.join(os.path.dirname(__file__), "backtest_cache.db"),
    "symbols": ["BTC", "ETH"],
    "intervals": ["1m"],
    # Fetch from May 21 to May 24 to have plenty of lookback data
    "start_date": "2026-05-21",
    "end_date": "2026-05-24",
    "target_day": "2026-05-23",
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
    "base_entry_price": 0.48, # Median entry price
    "option_c_coeff": 5.0,     # Option C multiplier for first minute return
    "position_size_usdc": 10.0,
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

# --- Binance Kline Fetcher ---
def fetch_binance_klines(symbol, start_ms, end_ms):
    import requests
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
            print(f"    Progress: fetched {len(klines)} candles, up to {datetime.fromtimestamp(current_start/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")
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
        # If cache covers our requested range
        if c_min <= start_ms + 60000 and c_max >= end_ms - 60000:
            print(f"  Cache HIT for {symbol} ({count} rows). No API call needed.")
            need_fetch = False
            
    if need_fetch:
        print(f"  Cache MISS for {symbol}. Fetching range {start_date} to {end_date}...")
        fetch_start = start_ms - 4 * 3600 * 1000
        fetch_end = end_ms + 4 * 3600 * 1000
        klines = fetch_binance_klines(symbol, fetch_start, fetch_end)
        if klines:
            save_klines(conn, symbol, klines)
            print(f"  Successfully cached {len(klines)} rows for {symbol}.")
            
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
    
    if open_1m <= 0:
        return config["base_entry_price"]
        
    ret_1m = (close_1m - open_1m) / open_1m
    target_sign = 1 if target_side == "YES" else -1
    
    coeff = config["option_c_coeff"]
    simulated_price = config["base_entry_price"] - (target_sign * coeff * ret_1m)
    return float(np.clip(simulated_price, 0.01, 0.99))

# --- Backtest Engine ---
def run_backtest(df_1m, symbol, window_minutes, config):
    df_window = resample_klines(df_1m, window_minutes)
    
    trades = []
    last_traded_close_ts = 0
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    # Target day range
    target_dt = datetime.strptime(config["target_day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    target_start_ts = int(target_dt.timestamp())
    target_end_ts = int((target_dt + timedelta(days=1)).timestamp()) - 1
    
    for i in range(3, len(df_window)):
        row = df_window.iloc[i]
        closes_at = row["ts_close"]
        open_at = row["ts_open"]
        
        # Streak detection (Streak-2)
        lookback1 = df_window.iloc[i-1]
        lookback2 = df_window.iloc[i-2]
        
        if lookback1["outcome"] != lookback2["outcome"]:
            continue
            
        streak_dir = lookback1["outcome"]
        target_side = "YES" if streak_dir == "UP" else "NO"
        
        # Streak Reset Guard
        m_prev2_close = lookback2["ts_close"]
        if m_prev2_close <= last_traded_close_ts:
            continue
            
        # Simulate Entry Price
        open_time_ms = open_at * 1000
        first_min_rows = df_1m[df_1m["open_time"] == open_time_ms]
        first_min_row = first_min_rows.iloc[0] if not first_min_rows.empty else None
        entry_price = estimate_entry_price(first_min_row, target_side, config)
        
        # Entry price filter
        if entry_price < config["min_entry_price"] or entry_price > config["max_entry_price"]:
            continue
            
        # Filter: Only trade during the target day
        if open_at < target_start_ts or open_at > target_end_ts:
            continue
            
        # Resolution
        actual_outcome = row["outcome"]
        win = (target_side == "YES" and actual_outcome == "UP") or (target_side == "NO" and actual_outcome == "DOWN")
        
        payout = 1.0 if win else 0.0
        
        # Exact share rounding (from live bot)
        raw_shares = config["position_size_usdc"] / entry_price
        shares = int(raw_shares)
        
        # Clamp to minimum order size if needed (5 shares)
        if shares < 5:
            shares = 5
            
        actual_size_usdc = shares * entry_price
        pnl = (shares * payout) - actual_size_usdc
        
        trade_time_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
        
        trades.append({
            "open_at": open_at,
            "timestamp_utc": trade_time_utc,
            "timestamp_utc_str": trade_time_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "symbol": symbol,
            "window": window_minutes,
            "streak": f"{lookback1['outcome']}-{lookback2['outcome']}",
            "side": target_side,
            "entry_price": entry_price,
            "outcome": actual_outcome,
            "win": win,
            "shares": shares,
            "actual_size_usdc": actual_size_usdc,
            "pnl": pnl,
        })
        
        last_traded_close_ts = closes_at
        
    return trades

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

def main():
    print("=" * 80)
    print(f"STREAK MOMENTUM BACKTESTER - AUDITING {CONFIG['target_day']}")
    print("=" * 80)
    
    # 1. Load actual bot trades
    actual_file_path = os.path.join(os.path.dirname(__file__), "may23_trades.json")
    if not os.path.exists(actual_file_path):
        print(f"ERROR: Local actual trades file not found at {actual_file_path}")
        print("Please copy may23_trades.json from the VPS first.")
        sys.exit(1)
        
    with open(actual_file_path, "r") as f:
        bot_trades = json.load(f)
        
    print(f"Loaded {len(bot_trades)} actual paper trades from VPS DB.")
    
    # Parse bot trades to extract symbol, window, and side
    for bt in bot_trades:
        slug = bt["slug"].lower()
        question = bt["question"].lower()
        
        # Asset
        if slug.startswith("btc") or "bitcoin" in question or "btc" in question:
            bt["derived_symbol"] = "BTC"
        elif slug.startswith("eth") or "ethereum" in question or "eth" in question:
            bt["derived_symbol"] = "ETH"
        else:
            bt["derived_symbol"] = "UNKNOWN"
            
        # Window
        if "-5m-" in slug or "5-minute" in question:
            bt["derived_window"] = 5
        elif "-15m-" in slug or "15-minute" in question:
            bt["derived_window"] = 15
        else:
            bt["derived_window"] = 5 # default fallback
            
    # 2. Run Backtest
    conn = init_db(CONFIG["db_path"])
    data_cache = {}
    for symbol in CONFIG["symbols"]:
        df_1m = load_data(conn, symbol, CONFIG["start_date"], CONFIG["end_date"])
        data_cache[symbol] = df_1m
    conn.close()
    
    backtest_trades = []
    for symbol in CONFIG["symbols"]:
        df_1m = data_cache[symbol]
        for window in [5, 15]:
            bt = run_backtest(df_1m, symbol, window, CONFIG)
            backtest_trades.extend(bt)
            
    # Sort backtest trades by timestamp
    backtest_trades.sort(key=lambda x: x["open_at"])
    print(f"Backtester generated {len(backtest_trades)} simulated trades for {CONFIG['target_day']}.")
    
    # 3. Perform Match Audit
    # We match a backtest trade to a bot trade if:
    # - Symbol matches
    # - Window matches
    # - Side matches
    # - Timestamp difference is within 90 seconds (since live bot might enter slightly late)
    matched_backtest_indices = set()
    matched_bot_indices = set()
    matches = []
    
    for i, btt in enumerate(backtest_trades):
        best_match_idx = None
        min_diff = 9999
        
        for j, bot_t in enumerate(bot_trades):
            if j in matched_bot_indices:
                continue
                
            if (btt["symbol"] == bot_t["derived_symbol"] and 
                btt["window"] == bot_t["derived_window"] and 
                btt["side"] == bot_t["side"]):
                
                # Check timestamp difference
                diff = abs(btt["open_at"] - bot_t["opened_at"])
                if diff <= 90 and diff < min_diff:
                    min_diff = diff
                    best_match_idx = j
                    
        if best_match_idx is not None:
            matched_backtest_indices.add(i)
            matched_bot_indices.add(best_match_idx)
            matches.append((btt, bot_trades[best_match_idx]))
            
    # Calculate stats
    total_backtest = len(backtest_trades)
    total_bot = len(bot_trades)
    total_matches = len(matches)
    
    backtest_pnl = sum(t["pnl"] for t in backtest_trades)
    bot_pnl = sum(t["realised_pnl"] for t in bot_trades)
    
    match_rate_backtest = (total_matches / total_backtest * 100) if total_backtest > 0 else 0.0
    match_rate_bot = (total_matches / total_bot * 100) if total_bot > 0 else 0.0
    
    # Unmatched trades
    unmatched_backtest = [btt for idx, btt in enumerate(backtest_trades) if idx not in matched_backtest_indices]
    unmatched_bot = [bot_t for idx, bot_t in enumerate(bot_trades) if idx not in matched_bot_indices]
    
    # Price difference analysis on matched trades
    price_diffs = []
    pnl_diffs = []
    win_mismatches = 0
    
    for btt, bot_t in matches:
        price_diff = btt["entry_price"] - bot_t["entry_price"]
        price_diffs.append(price_diff)
        pnl_diff = btt["pnl"] - bot_t["realised_pnl"]
        pnl_diffs.append(pnl_diff)
        
        # Verify if resolution matched
        if btt["win"] != (bot_t["realised_pnl"] > 0):
            win_mismatches += 1
            
    avg_price_diff = np.mean(price_diffs) if price_diffs else 0.0
    mae_price_diff = np.mean(np.abs(price_diffs)) if price_diffs else 0.0
    mae_pnl_diff = np.mean(np.abs(pnl_diffs)) if pnl_diffs else 0.0
    
    # 4. Generate Markdown Report
    report_path = os.path.join(os.path.dirname(__file__), "backtest_results_20260523.md")
    
    report_md = f"""# May 23, 2026 Mimic Verification & Backtest Report

Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
Target Audit Date: **{CONFIG['target_day']}**

## 📊 Executive Summary

This report performs a trade-by-trade audit comparing the **V3 Backtester** (simulated from 1m Binance klines using Option C entry pricing and integer share rounding) against the **Actual Paper Trades** executed by the live Reversebot on the VPS.

| Metric | Backtester (Simulated) | Live Bot (Actual Paper) | Discrepancy |
| --- | --- | --- | --- |
| **Total Trades** | {total_backtest} | {total_bot} | {total_bot - total_backtest:+} trades |
| **Wins** | {sum(1 for t in backtest_trades if t['win'])} | {sum(1 for t in bot_trades if t['realised_pnl'] > 0)} | {sum(1 for t in bot_trades if t['realised_pnl'] > 0) - sum(1 for t in backtest_trades if t['win']):+} wins |
| **Win Rate** | {sum(1 for t in backtest_trades if t['win']) / total_backtest * 100 if total_backtest > 0 else 0.0:.2f}% | {sum(1 for t in bot_trades if t['realised_pnl'] > 0) / total_bot * 100 if total_bot > 0 else 0.0:.2f}% | {sum(1 for t in bot_trades if t['realised_pnl'] > 0) / total_bot * 100 - sum(1 for t in backtest_trades if t['win']) / total_backtest * 100 if total_bot > 0 and total_backtest > 0 else 0.0:+.2f}% |
| **Total P&L** | {backtest_pnl:+.2f} USDC | {bot_pnl:+.2f} USDC | {bot_pnl - backtest_pnl:+.2f} USDC |

---

## 🔍 Mimic & Fidelity Audit

- **Total Matched Trades**: {total_matches}
- **Fidelity Match Rate (Backtest)**: {match_rate_backtest:.1f}% (Matched {total_matches} / {total_backtest} simulated trades)
- **Fidelity Match Rate (Bot)**: {match_rate_bot:.1f}% (Matched {total_matches} / {total_bot} actual trades)
- **Win/Loss Resolution Mismatches**: {win_mismatches} (Trades where backtester and bot had different outcomes)
- **Average Entry Price Discrepancy (Simulated - Actual)**: {avg_price_diff:+.4f}
- **Mean Absolute Error (MAE) on Entry Price**: {mae_price_diff:.4f} USDC
- **Mean Absolute Error (MAE) on Trade P&L**: {mae_pnl_diff:.2f} USDC

### ⚖️ Verdict: Does the Backtester Mimic the Bot?

> [!NOTE]
> **Fidelity Verdict**: **{ 'HIGH FIDELITY (>= 80% Match)' if match_rate_bot >= 80 else 'MODERATE FIDELITY (50% - 79% Match)' if match_rate_bot >= 50 else 'LOW FIDELITY (< 50% Match)' }**
> 
> **Fidelity Match Rate (Bot)** is **{match_rate_bot:.2f}%**.
> **Option C Price Simulation MAE**: **{mae_price_diff:.4f}**. This demonstrates that the Option C price simulation (first 1-minute Binance candle return proxy) is { 'a strong proxy' if mae_price_diff <= 0.03 else 'a moderate proxy' if mae_price_diff <= 0.06 else 'a weak proxy' } for real order-book entry prices on Polymarket.

---

## 🔬 Discrepancy Details

### ⚠️ Bot Trades NOT Found in Backtest (Missing in Backtest)
*Count: {len(unmatched_bot)}*

The following actual trades were taken by the bot but NOT simulated in the backtest. This typically occurs due to:
1. **Spread / Slippage Filter Difference**: The backtester assumes a single price, whereas the bot rejects trades if the order book spread is too wide.
2. **Timing & Scanner Lag**: Minor differences in when kline data becomes available vs when the bot pulls Gamma API.

{to_markdown_table(pd.DataFrame([{
    "Time (UTC)": t["opened_at_utc"],
    "Asset": t["derived_symbol"],
    "Window": f"{t['derived_window']}m",
    "Side": t["side"],
    "Entry (Actual)": f"${t['entry_price']:.2f}",
    "PnL (Actual)": f"${t['realised_pnl']:+.2f}"
} for t in unmatched_bot])) if unmatched_bot else "None! Perfect matching."}

### ⚠️ Backtest Trades NOT Found in Bot (Extra in Backtest)
*Count: {len(unmatched_backtest)}*

The following trades were simulated in the backtester but NOT taken by the live bot. This typically occurs because:
1. **No Market / Zero Volume**: The backtest checks all historical klines, but on Polymarket, a specific 5m or 15m window market might not have been created or active at that time.
2. **Concurrency / Exposure Limits**: The bot might have hit its `max_open_positions` limit (50) or capital limits.

{to_markdown_table(pd.DataFrame([{
    "Time (UTC)": t["timestamp_utc_str"],
    "Asset": t["symbol"],
    "Window": f"{t['window']}m",
    "Side": t["side"],
    "Entry (Simulated)": f"${t['entry_price']:.2f}",
    "PnL (Simulated)": f"${t['pnl']:+.2f}"
} for t in unmatched_backtest])) if unmatched_backtest else "None! Perfect matching."}

---

## 📈 Detailed Matched Trade Sample

Below is a comparison of matched trades (first 25 shown):

{to_markdown_table(pd.DataFrame([{
    "Time (UTC)": btt["timestamp_utc_str"],
    "Asset": btt["symbol"],
    "Window": f"{btt['window']}m",
    "Side": btt["side"],
    "Entry (Sim)": f"${btt['entry_price']:.2f}",
    "Entry (Actual)": f"${bot_t['entry_price']:.2f}",
    "Price Diff": f"{btt['entry_price'] - bot_t['entry_price']:+.2f}",
    "PnL (Sim)": f"${btt['pnl']:+.2f}",
    "PnL (Actual)": f"${bot_t['realised_pnl']:+.2f}"
} for btt, bot_t in matches[:25]])) if matches else "No matched trades."}
"""
    
    with open(report_path, "w") as f:
        f.write(report_md)
        
    print(f"\nAudit completed. Report written to: {report_path}")
    print(f"Summary stats:")
    print(f"  Backtest Trades: {total_backtest} (PnL: {backtest_pnl:+.2f} USDC)")
    print(f"  Bot Trades:      {total_bot} (PnL: {bot_pnl:+.2f} USDC)")
    print(f"  Matches:         {total_matches} ({match_rate_bot:.1f}% matching rate)")

if __name__ == "__main__":
    main()
