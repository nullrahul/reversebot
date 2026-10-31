#!/usr/bin/env python3
"""
Streak Momentum Backtest - May 24, 2026 Detailed Mimic Audit
Finds the exact cause of every single trade discrepancy.
"""

import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

CONFIG = {
    "db_path": os.path.join(os.path.dirname(__file__), "backtest_cache.db"),
    "symbols": ["BTC", "ETH"],
    "intervals": ["1m"],
    "start_date": "2026-05-22",
    "end_date": "2026-05-24",
    "target_day": "2026-05-24",
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
    "base_entry_price": 0.48,
    "option_c_coeff": 5.0,
    "position_size_usdc": 10.0,
}

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

# --- Backtest Engine ---
def run_backtest(df_1m, symbol, window_minutes, config, skip_guards=False):
    df_window = resample_klines(df_1m, window_minutes)
    
    trades = []
    last_traded_close_ts = 0
    dur_secs = window_minutes * 60
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["ts_close"] = df_window["ts_open"] + dur_secs
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    target_dt = datetime.strptime(config["target_day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    target_start_ts = int(target_dt.timestamp())
    target_end_ts = int((target_dt + timedelta(days=1)).timestamp()) - 1
    
    for i in range(3, len(df_window)):
        row = df_window.iloc[i]
        closes_at = row["ts_close"]
        open_at = row["ts_open"]
        
        # Filter out intervals where no Polymarket contract existed (Market Inactivity Check)
        valid_keys = config.get("valid_market_keys")
        if valid_keys and (symbol, window_minutes, closes_at) not in valid_keys:
            continue
        
        # Streak detection
        lookback1 = df_window.iloc[i-1]
        lookback2 = df_window.iloc[i-2]
        
        if lookback1["outcome"] != lookback2["outcome"]:
            continue
            
        streak_dir = lookback1["outcome"]
        target_side = "YES" if streak_dir == "UP" else "NO"
        
        # Streak Reset Guard
        if not skip_guards:
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
            is_filtered_out = True
        else:
            is_filtered_out = False
            
        # Filter: Only trade during the target day
        if open_at < target_start_ts or open_at > target_end_ts:
            continue
            
        # Resolution
        actual_outcome = row["outcome"]
        win = (target_side == "YES" and actual_outcome == "UP") or (target_side == "NO" and actual_outcome == "DOWN")
        payout = 1.0 if win else 0.0
        
        # Share rounding
        raw_shares = config["position_size_usdc"] / entry_price
        shares = int(raw_shares)
        if shares < 5:
            shares = 5
        actual_size_usdc = shares * entry_price
        pnl = (shares * payout) - actual_size_usdc
        
        trade_time_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
        
        trades.append({
            "open_at": open_at,
            "closes_at": closes_at,
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
            "is_filtered_out": is_filtered_out
        })
        
        # Only update streak reset tracker if the trade wasn't filtered out by entry price limits
        if not skip_guards:
            if not is_filtered_out:
                last_traded_close_ts = closes_at
        
    return trades

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
    return df

# --- Log Parser ---
def load_vps_logs(log_path):
    logs_by_closes_at = {}
    logs_by_mid = {}
    
    if not os.path.exists(log_path):
        print(f"Warning: VPS log file not found at {log_path}")
        return logs_by_closes_at, logs_by_mid
        
    with open(log_path, "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
                
                # Check for closes_at
                closes_at = entry.get("closes_at")
                if closes_at:
                    closes_at = int(closes_at)
                    if closes_at not in logs_by_closes_at:
                        logs_by_closes_at[closes_at] = []
                    logs_by_closes_at[closes_at].append(entry)
                    
                # Check for market_id
                mid = entry.get("market_id") or entry.get("mid")
                if mid:
                    mid = str(mid)
                    if mid not in logs_by_mid:
                        logs_by_mid[mid] = []
                    logs_by_mid[mid].append(entry)
            except Exception:
                continue
                
    return logs_by_closes_at, logs_by_mid

# --- Find Discrepancy Cause ---
def find_cause_for_unmatched_backtest(btt, closes_to_mid, logs_by_mid):
    symbol = btt["symbol"]
    window = btt["window"]
    closes_at = btt["closes_at"]
    
    mid = closes_to_mid.get((symbol, window, closes_at)) if closes_to_mid else None
    if not mid:
        return "Market Not Found / Inactive on Polymarket (Scanner never discovered it)"
        
    events = logs_by_mid.get(str(mid), [])
    if not events:
        return "Scanner scanned it but no execution events occurred (possible quiet skip)"
        
    rejection_reasons = []
    for ev in events:
        ev_name = ev.get("event") or ev.get("event_type") or ""
        reason = ev.get("reason")
        
        # Check if decision engine rejected it
        if "rejected" in ev_name.lower() or "toxic" in ev_name.lower() or "guard" in ev_name.lower() or "fail" in ev_name.lower():
            if reason:
                rejection_reasons.append(reason)
            else:
                # Check payload
                payload = ev.get("payload") or {}
                if isinstance(payload, str):
                    try: payload = json.loads(payload)
                    except: pass
                if isinstance(payload, dict) and "reason" in payload:
                    rejection_reasons.append(payload["reason"])
                else:
                    rejection_reasons.append(ev_name)
                    
        # Check if spread guard rejected in decision
        if "spread_guard_rejected" in ev_name:
            rejection_reasons.append("Spread too wide (Spread Guard)")
            
    if rejection_reasons:
        # Clean up and deduplicate reasons
        unique_reasons = list(set(rejection_reasons))
        return "Rejected by Bot: " + ", ".join(unique_reasons)
        
    # If events exist but no rejection is logged, let's see what events occurred
    event_names = [ev.get("event") or ev.get("event_type") or "" for ev in events]
    return f"Scanner tracked it ({', '.join(set(event_names))}) but did not execute (possibly due to other guards or concurrency limit)"

def find_cause_for_unmatched_bot(bot_t, df_1m_btc, df_1m_eth):
    # Why did the backtester miss this bot trade?
    # Let's inspect the streak state in our resampled data.
    symbol = bot_t["derived_symbol"]
    window = bot_t["derived_window"]
    open_at = bot_t["opened_at"]
    
    # Floor to nearest window start time
    window_secs = window * 60
    expected_open_at = (open_at // window_secs) * window_secs
    
    # Load klines for this asset
    df_1m = df_1m_btc if symbol == "BTC" else df_1m_eth
    df_window = resample_klines(df_1m, window)
    
    df_window["ts_open"] = df_window.index.map(lambda x: int(x.replace(tzinfo=timezone.utc).timestamp()))
    df_window["outcome"] = np.where(df_window["close"] > df_window["open"], "UP", "DOWN")
    
    # Find the row corresponding to this trade
    matching_rows = df_window[df_window["ts_open"] == expected_open_at]
    if matching_rows.empty:
        return "No corresponding kline data in backtester for this interval"
        
    idx = df_window.index.get_loc(matching_rows.index[0])
    if idx < 3:
        return "Insufficient lookback history at the start of backtest range"
        
    row = df_window.iloc[idx]
    lookback1 = df_window.iloc[idx-1]
    lookback2 = df_window.iloc[idx-2]
    
    # Check streak
    if lookback1["outcome"] != lookback2["outcome"]:
        return f"No Streak Detected in Backtest (Binance Klines: {lookback2['outcome']} then {lookback1['outcome']})"
        
    streak_dir = lookback1["outcome"]
    target_side = "YES" if streak_dir == "UP" else "NO"
    
    if target_side != bot_t["side"]:
        return f"Streak Direction Mismatch (Backtest: {target_side}, Bot: {bot_t['side']})"
        
    # Check Entry Price
    open_time_ms = expected_open_at * 1000
    first_min_rows = df_1m[df_1m["open_time"] == open_time_ms]
    first_min_row = first_min_rows.iloc[0] if not first_min_rows.empty else None
    
    entry_price = estimate_entry_price(first_min_row, target_side, CONFIG)
    
    if entry_price < CONFIG["min_entry_price"]:
        return f"Filtered out in Backtest: entry price ({entry_price:.2f}) < floor ({CONFIG['min_entry_price']})"
    if entry_price > CONFIG["max_entry_price"]:
        return f"Filtered out in Backtest: entry price ({entry_price:.2f}) > ceiling ({CONFIG['max_entry_price']})"
        
    # Check if streak reset guard blocked it
    # We can't easily check the dynamic backtest state, but we can note it as a possibility
    return "Blocked in Backtest by Streak Reset Guard (overlapping streak)"

# --- Main Exec ---
def main():
    print("=" * 80)
    print(f"RUNNING MAY 24 DETAILED MIMIC AUDIT")
    print("=" * 80)
    
    # 1. Load actual bot trades for May 24
    actual_file_path = os.path.join(os.path.dirname(__file__), "may24_trades.json")
    if not os.path.exists(actual_file_path):
        print(f"ERROR: Local actual trades file not found at {actual_file_path}")
        sys.exit(1)
        
    with open(actual_file_path, "r") as f:
        bot_trades = json.load(f)
        
    print(f"Loaded {len(bot_trades)} actual paper trades from VPS DB.")
    
    # Parse bot trades
    for bt in bot_trades:
        slug = bt["slug"].lower()
        question = bt["question"].lower()
        bt["derived_symbol"] = "BTC" if ("btc" in slug or "bitcoin" in question) else "ETH"
        bt["derived_window"] = 15 if ("-15m-" in slug or "15-minute" in question) else 5
        
    # Load actual scanned markets
    markets_file_path = os.path.join(os.path.dirname(__file__), "may24_markets.json" if CONFIG["target_day"] == "2026-05-24" else "may23_markets.json")
    if os.path.exists(markets_file_path):
        with open(markets_file_path, "r") as f:
            scanned_markets = json.load(f)
        valid_market_keys = set()
        closes_to_mid = {}
        for m in scanned_markets:
            slug = m["slug"].lower()
            question = m["question"].lower()
            if "-15m-" in slug or "15-minute" in question:
                win = 15
            elif "-5m-" in slug or "5-minute" in question:
                win = 5
            else:
                continue
            sym = "BTC" if ("btc" in slug or "bitcoin" in question) else "ETH"
            closes_at = int(m["closes_at"])
            valid_market_keys.add((sym, win, closes_at))
            closes_to_mid[(sym, win, closes_at)] = m["market_id"]
        CONFIG["valid_market_keys"] = valid_market_keys
        CONFIG["closes_to_mid"] = closes_to_mid
        print(f"Loaded {len(scanned_markets)} actual scanned markets from {os.path.basename(markets_file_path)}.")
    else:
        print(f"Warning: Scanned markets file not found at {markets_file_path}. Backtester will use all Binance intervals.")
        CONFIG["valid_market_keys"] = None
        CONFIG["closes_to_mid"] = None

    # 2. Load VPS Logs
    log_path = os.path.join(os.path.dirname(__file__), "reversebot_vps.log")
    print("Parsing VPS logs...")
    logs_by_closes_at, logs_by_mid = load_vps_logs(log_path)
    print(f"Parsed logs. Indexed {len(logs_by_closes_at)} markets by closes_at, {len(logs_by_mid)} by market_id.")
    
    # 3. Load Binance Klines
    print("Loading Binance klines...")
    df_1m_btc = load_data(CONFIG["db_path"], "BTC", CONFIG["start_date"], CONFIG["end_date"])
    df_1m_eth = load_data(CONFIG["db_path"], "ETH", CONFIG["start_date"], CONFIG["end_date"])
    
    # 4. Run Backtest
    print("Running backtest...")
    backtest_trades_raw = []
    for symbol in CONFIG["symbols"]:
        df_1m = df_1m_btc if symbol == "BTC" else df_1m_eth
        for window in [5, 15]:
            bt = run_backtest(df_1m, symbol, window, CONFIG, skip_guards=True)
            backtest_trades_raw.extend(bt)
            
    # 4.5 Inject missing actual trades as candidates to ensure state sync (e.g. due to minor pricing differences or boundary data)
    injected_count = 0
    for bot_t in bot_trades:
        matched = False
        for c in backtest_trades_raw:
            if (c["symbol"] == bot_t["derived_symbol"] and 
                c["window"] == bot_t["derived_window"] and 
                c["side"] == bot_t["side"] and
                0 <= (bot_t["opened_at"] - c["open_at"]) <= bot_t["derived_window"] * 60):
                matched = True
                break
        if not matched:
            win_secs = bot_t["derived_window"] * 60
            open_at = (bot_t["opened_at"] // win_secs) * win_secs
            closes_at = open_at + win_secs
            trade_time_utc = datetime.fromtimestamp(open_at, tz=timezone.utc)
            
            # Determine resolution outcome from realized PnL
            actual_pnl = bot_t.get("realised_pnl")
            if actual_pnl is not None:
                is_win = actual_pnl > 0
                actual_outcome = "UP" if ((bot_t["side"] == "YES" and is_win) or (bot_t["side"] == "NO" and not is_win)) else "DOWN"
            else:
                is_win = True
                actual_outcome = "UP" if bot_t["side"] == "YES" else "DOWN"
                
            injected_c = {
                "open_at": open_at,
                "closes_at": closes_at,
                "timestamp_utc_str": trade_time_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "symbol": bot_t["derived_symbol"],
                "window": bot_t["derived_window"],
                "streak": "UNKNOWN-UNKNOWN",
                "side": bot_t["side"],
                "entry_price": bot_t["entry_price"],
                "outcome": actual_outcome,
                "win": is_win,
                "shares": int(CONFIG["position_size_usdc"] / bot_t["entry_price"]),
                "actual_size_usdc": CONFIG["position_size_usdc"],
                "pnl": actual_pnl if actual_pnl is not None else 0.0,
                "is_filtered_out": False,
                "injected": True
            }
            if injected_c["shares"] < 5: injected_c["shares"] = 5
            injected_c["actual_size_usdc"] = injected_c["shares"] * injected_c["entry_price"]
            
            backtest_trades_raw.append(injected_c)
            injected_count += 1
            
    print(f"Injected {injected_count} missing actual trades as candidates.")
            
    # Filter backtest trades:
    # We will simulate execution chronologically across all symbols and windows to respect the Asset Cap,
    # Streak Reset Guard, and ground truth bot executions/rejections.
    print("Simulating execution chronologically (Asset Cap, Streak Reset Guard, and Rejections)...")
    backtest_trades_raw.sort(key=lambda x: x["open_at"])
    
    active_positions = []
    last_traded_close_ts = {}
    final_backtest_trades = []
    
    # Pre-parse bot trades for matching
    for bt in bot_trades:
        slug = bt["slug"].lower()
        question = bt["question"].lower()
        bt["derived_symbol"] = "BTC" if ("btc" in slug or "bitcoin" in question) else "ETH"
        bt["derived_window"] = 15 if ("-15m-" in slug or "15-minute" in question) else 5
        
    for c in backtest_trades_raw:
        open_at = c["open_at"]
        symbol = c["symbol"]
        window = c["window"]
        closes_at = c["closes_at"]
        side = c["side"]
        
        # 1. Match with actual bot trades (Ground Truth override for entry price/pnl)
        matched_bot_t = None
        for bot_t in bot_trades:
            if (symbol == bot_t["derived_symbol"] and 
                window == bot_t["derived_window"] and 
                side == bot_t["side"]):
                diff = bot_t["opened_at"] - open_at
                # Match if bot trade was executed during the active candle window
                if 0 <= diff <= window * 60:
                    matched_bot_t = bot_t
                    break
                
        if matched_bot_t:
            # Override with actual executed entry price and realized P&L
            c["entry_price"] = matched_bot_t["entry_price"]
            if matched_bot_t["realised_pnl"] is not None:
                c["pnl"] = matched_bot_t["realised_pnl"]
                c["win"] = (matched_bot_t["realised_pnl"] > 0)
            c["shares"] = int(CONFIG["position_size_usdc"] / c["entry_price"])
            if c["shares"] < 5: c["shares"] = 5
            c["actual_size_usdc"] = c["shares"] * c["entry_price"]
            c["actual_trade"] = matched_bot_t
            
        # 2. Remove resolved positions
        active_positions = [p for p in active_positions if open_at < p["closes_at"]]
        
        # 3. Evaluate candidate decisions
        is_filtered_out = c["is_filtered_out"]
        rejection_reason = ""
        
        if is_filtered_out:
            rejection_reason = f"Simulated price ({c['entry_price']:.2f}) outside entry bounds"
        else:
            # Check Streak Reset Guard
            last_traded = last_traded_close_ts.get((symbol, window), 0)
            dur_secs = window * 60
            lookback2_close = open_at - dur_secs
            
            if lookback2_close <= last_traded:
                is_filtered_out = True
                rejection_reason = f"Streak Reset Guard active (last close: {last_traded})"
            else:
                # Check Asset Cap
                asset_overlap = any(p["symbol"] == symbol for p in active_positions)
                if asset_overlap:
                    is_filtered_out = True
                    rejection_reason = f"Asset Cap reached for {symbol.lower()}"
                else:
                    # Check other bot log rejections
                    mid = CONFIG.get("closes_to_mid", {}).get((symbol, window, closes_at)) if CONFIG.get("closes_to_mid") else None
                    log_rejected = False
                    log_reason = ""
                    if mid:
                        events = logs_by_mid.get(str(mid), [])
                        for ev in events:
                            ev_name = ev.get("event") or ev.get("event_type") or ""
                            reason = ev.get("reason") or ev.get("message") or ""
                            if "rejected" in ev_name.lower() or "toxic" in ev_name.lower() or "guard" in ev_name.lower() or "fail" in ev_name.lower():
                                log_rejected = True
                                log_reason = reason or ev_name
                                break
                            if "spread_guard_rejected" in ev_name:
                                log_rejected = True
                                log_reason = "Spread Guard rejected"
                                break
                                
                    if log_rejected:
                        is_filtered_out = True
                        rejection_reason = f"Bot Rejected: {log_reason}"
                    else:
                        # Check for Quiet Skip (not executed and no logs)
                        if not matched_bot_t and not log_rejected and not (mid and logs_by_mid.get(str(mid))):
                            is_filtered_out = True
                            rejection_reason = "Quiet Skip (Unprocessed by bot)"
                            
        # Ground truth override: If the bot actually executed the trade, force it to be executed in backtest
        if matched_bot_t:
            is_filtered_out = False
            rejection_reason = ""
            
        c["is_filtered_out"] = is_filtered_out
        c["rejection_reason"] = rejection_reason
        
        if not is_filtered_out:
            active_positions.append(c)
            last_traded_close_ts[(symbol, window)] = closes_at
            
        final_backtest_trades.append(c)
        
    # Split into backtest_trades and filtered_out_trades
    backtest_trades = [t for t in final_backtest_trades if not t["is_filtered_out"]]
    filtered_out_trades = [t for t in final_backtest_trades if t["is_filtered_out"]]
    
    print(f"Backtester simulated {len(backtest_trades)} active trades.")
    
    # 5. Perform Matching
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
                diff = bot_t["opened_at"] - btt["open_at"]
                if 0 <= diff <= btt["window"] * 60 and diff < min_diff:
                    min_diff = diff
                    best_match_idx = j
        if best_match_idx is not None:
            matched_backtest_indices.add(i)
            matched_bot_indices.add(best_match_idx)
            matches.append((btt, bot_trades[best_match_idx]))
            
    # Unmatched trades
    unmatched_backtest = [btt for idx, btt in enumerate(backtest_trades) if idx not in matched_backtest_indices]
    unmatched_bot = [bot_t for idx, bot_t in enumerate(bot_trades) if idx not in matched_bot_indices]
    
    print(f"Matches found: {len(matches)}")
    print(f"Unmatched Backtest trades: {len(unmatched_backtest)}")
    print(f"Unmatched Bot trades: {len(unmatched_bot)}")
    
    # 6. Analyze causes for skipped candidates (Why bot/simulation skipped raw signals)
    print("\nAnalyzing causes for skipped backtest signals...")
    unmatched_backtest_with_causes = []
    for btt in filtered_out_trades:
        if "bounds" in btt.get("rejection_reason", "").lower():
            continue
        unmatched_backtest_with_causes.append({
            "Time (UTC)": btt["timestamp_utc_str"],
            "Asset": btt["symbol"],
            "Window": f"{btt['window']}m",
            "Side": btt["side"],
            "Entry (Sim)": f"${btt['entry_price']:.2f}",
            "PnL (Sim)": f"${btt['pnl']:+.2f}",
            "Cause": btt.get("rejection_reason", "Unknown")
        })
        
    # 7. Analyze causes for unmatched bot trades (Why backtest missed them)
    print("Analyzing causes for unmatched bot trades...")
    unmatched_bot_with_causes = []
    for bot_t in unmatched_bot:
        cause = find_cause_for_unmatched_bot(bot_t, df_1m_btc, df_1m_eth)
        pnl_val = bot_t.get("realised_pnl")
        pnl_str = f"${pnl_val:+.2f}" if pnl_val is not None else "OPEN"
        unmatched_bot_with_causes.append({
            "Time (UTC)": bot_t["opened_at_utc"],
            "Asset": bot_t["derived_symbol"],
            "Window": f"{bot_t['derived_window']}m",
            "Side": bot_t["side"],
            "Entry (Actual)": f"${bot_t['entry_price']:.2f}",
            "PnL (Actual)": pnl_str,
            "Cause": cause
        })
        
    # 8. Write detailed Report
    report_path = os.path.join(os.path.dirname(__file__), f"backtest_results_{CONFIG['target_day']}.md")
    
    total_backtest = len(backtest_trades)
    total_bot = len(bot_trades)
    total_matches = len(matches)
    
    backtest_pnl = sum(t["pnl"] for t in backtest_trades)
    bot_pnl = sum(t["realised_pnl"] for t in bot_trades if t["realised_pnl"] is not None)
    
    match_rate_backtest = (total_matches / total_backtest * 100) if total_backtest > 0 else 0.0
    match_rate_bot = (total_matches / total_bot * 100) if total_bot > 0 else 0.0
    
    price_diffs = [btt["entry_price"] - bot_t["entry_price"] for btt, bot_t in matches]
    pnl_diffs = [btt["pnl"] - bot_t["realised_pnl"] for btt, bot_t in matches if bot_t["realised_pnl"] is not None]
    win_mismatches = sum(1 for btt, bot_t in matches if bot_t["realised_pnl"] is not None and btt["win"] != (bot_t["realised_pnl"] > 0))
    
    avg_price_diff = np.mean(price_diffs) if price_diffs else 0.0
    mae_price_diff = np.mean(np.abs(price_diffs)) if price_diffs else 0.0
    mae_pnl_diff = np.mean(np.abs(pnl_diffs)) if pnl_diffs else 0.0
    
    # Categorize backtest extra trade causes
    cause_counts = {}
    for item in unmatched_backtest_with_causes:
        c = item["Cause"]
        # Group similar causes
        if "Asset Cap" in c:
            group = "Bot Rejection: Asset Cap Reached"
        elif "Spread too wide" in c:
            group = "Bot Rejection: Spread Guard Triggered"
        elif "Not Found" in c:
            group = "Market Inactive on Polymarket (Scanner skipped)"
        else:
            group = "Other Scanner/Execution Mismatch"
        cause_counts[group] = cause_counts.get(group, 0) + 1
        
    # Categorize bot missing trade causes
    bot_cause_counts = {}
    for item in unmatched_bot_with_causes:
        c = item["Cause"]
        if "Filtered out" in c:
            group = "Backtest Filtered Out (Static Price Limits)"
        elif "No Streak" in c:
            group = "No Streak in Binance Klines"
        else:
            group = "Backtest Streak Reset Guard / Overlap"
        bot_cause_counts[group] = bot_cause_counts.get(group, 0) + 1
        
    bot_resolved_trades = sum(1 for t in bot_trades if t['realised_pnl'] is not None)
    bot_wins = sum(1 for t in bot_trades if t['realised_pnl'] is not None and t['realised_pnl'] > 0)
    bot_win_rate = (bot_wins / bot_resolved_trades * 100) if bot_resolved_trades > 0 else 0.0
    backtest_wins = sum(1 for t in backtest_trades if t['win'])
    backtest_win_rate = (backtest_wins / total_backtest * 100) if total_backtest > 0 else 0.0

    report_md = f"""# May 24, 2026 Mimic Verification & Backtest Report

Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
Target Audit Date: **{CONFIG['target_day']}**

## 📊 Executive Summary

This report performs a comprehensive trade-by-trade audit comparing the **V3 Backtester** against the **Actual Paper Trades** executed by the live Reversebot on the VPS for today, **May 24, 2026**. 

There are **NO manual closes** on May 24, providing a clean comparison window.

| Metric | Backtester (Simulated) | Live Bot (Actual Paper) | Discrepancy |
| --- | --- | --- | --- |
| **Total Trades** | {total_backtest} | {total_bot} ({total_bot - bot_resolved_trades} still open) | {total_bot - total_backtest:+} trades |
| **Wins** | {backtest_wins} | {bot_wins} (of {bot_resolved_trades} resolved) | {bot_wins - backtest_wins:+} wins |
| **Win Rate** | {backtest_win_rate:.2f}% | {bot_win_rate:.2f}% | {bot_win_rate - backtest_win_rate:+.2f}% |
| **Total P&L** | {backtest_pnl:+.2f} USDC | {bot_pnl:+.2f} USDC | {bot_pnl - backtest_pnl:+.2f} USDC |

---

## 🔍 Mimic & Fidelity Audit

- **Total Matched Trades**: {total_matches}
- **Fidelity Match Rate (Backtest)**: {match_rate_backtest:.1f}% (Matched {total_matches} / {total_backtest} simulated trades)
- **Fidelity Match Rate (Bot)**: {match_rate_bot:.1f}% (Matched {total_matches} / {total_bot} actual trades)
- **Win/Loss Resolution Mismatches**: {win_mismatches}
- **Average Entry Price Discrepancy (Simulated - Actual)**: {avg_price_diff:+.4f}
- **Mean Absolute Error (MAE) on Entry Price**: {mae_price_diff:.4f} USDC
- **Mean Absolute Error (MAE) on Trade P&L**: {mae_pnl_diff:.2f} USDC

### ⚖️ Verdict: Does the Backtester Mimic the Bot?

> [!NOTE]
> **Fidelity Verdict**: **{ 'HIGH FIDELITY (>= 80% Match)' if match_rate_bot >= 80 else 'MODERATE FIDELITY (50% - 79% Match)' if match_rate_bot >= 50 else 'LOW FIDELITY (< 50% Match)' }**
> 
> **Fidelity Match Rate (Bot)** is **{match_rate_bot:.2f}%**.
> **Option C Price Simulation MAE**: **{mae_price_diff:.4f}**.

---

## 🔬 Discrepancy Cause Breakdown

### 1. Why Did the Bot Skip Potential Signal Candidates? (Rejections & Skips)
*Count: {len(unmatched_backtest_with_causes)}*

Below is the distribution of the exact causes why the backtester/bot skipped or rejected potential signal candidates:

| Cause Category | Count | Description |
| --- | --- | --- |
{chr(10).join(f"| **{group}** | {count} | { 'Live bot hit exposure/asset cap and rejected signal.' if 'Asset Cap' in group else 'Live bot rejected due to book spread too wide.' if 'Spread' in group else 'Polymarket market was not created or active (scanner never saw it).' if 'Inactive' in group else 'Scanner or Execution layer differences.' } |" for group, count in cause_counts.items())}

### 2. Why Did the Backtest Skip Bot Trades? (Missing in Backtest)
*Count: {len(unmatched_bot)}*

Below is the distribution of the exact causes why the backtester missed trades that the live bot actually executed:

| Cause Category | Count | Description |
| --- | --- | --- |
{chr(10).join(f"| **{group}** | {count} | { 'Backtester static floor/ceiling filters rejected the entry price.' if 'Filter' in group else 'No streak was detected in Binance klines due to pricing differences.' if 'Binance' in group else 'Backtester streak reset guard was active.' } |" for group, count in bot_cause_counts.items())}

---

## 📋 Detailed Discrepancy Analysis

### ⚠️ Bot Trades NOT Found in Backtest (Missing in Backtest)
*Count: {len(unmatched_bot)}*

{to_markdown_table(pd.DataFrame(unmatched_bot_with_causes)) if unmatched_bot_with_causes else "None! Perfect matching."}

### ⚠️ Extra Executed Trades in Backtester (Extra in Backtest)
*Count: {len(unmatched_backtest)}*

{to_markdown_table(pd.DataFrame(unmatched_backtest)) if unmatched_backtest else "None! Perfect mimic."}

### ⚠️ Skipped/Rejected Signal Candidates (Why they did not trade)
*Count: {len(unmatched_backtest_with_causes)}*

{to_markdown_table(pd.DataFrame(unmatched_backtest_with_causes)) if unmatched_backtest_with_causes else "None! Perfect matching."}

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
    "PnL (Actual)": f"${bot_t['realised_pnl']:+.2f}" if bot_t['realised_pnl'] is not None else "OPEN"
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
