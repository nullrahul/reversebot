import os
import sqlite3
import pandas as pd
import sys
from datetime import datetime, timezone, timedelta

# Add path to import backtester
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "R_and_D", "backtesting")))
from streak_backtester_v3 import run_backtest, CONFIG, init_db, load_data

def calculate_clean_yesterday():
    # Set to yesterday
    CONFIG["start_date"] = "2026-05-22"
    CONFIG["end_date"] = "2026-05-22"
    
    db_path = CONFIG["db_path"]
    conn = sqlite3.connect(db_path)
    
    data_cache = {}
    for symbol in CONFIG["symbols"]:
        df_1m = load_data(conn, symbol, CONFIG["start_date"], CONFIG["end_date"])
        data_cache[symbol] = df_1m
    conn.close()
    
    # Run backtest for streak2
    all_trades = []
    for symbol in CONFIG["symbols"]:
        df_1m = data_cache[symbol]
        for window in [5, 15]:
            trades = run_backtest(df_1m, symbol, window, "streak2", CONFIG)
            all_trades.extend(trades)
            
    df = pd.DataFrame(all_trades)
    if df.empty:
        print("No trades found for yesterday.")
        return

    # THE TOXIC HOURS (ET) identified in the 23-day master report
    TOXIC_HOURS = [1, 3, 4, 8, 11, 12, 15, 16, 17, 20, 22, 23]
    
    # 1. Raw Stats
    total_trades = len(df)
    total_win = df["win"].sum()
    total_pnl = df["pnl"].sum()
    
    # 2. Clean Stats (Filtering out Toxic Hours)
    df_clean = df[~df["hour_et"].isin(TOXIC_HOURS)]
    clean_trades = len(df_clean)
    clean_win = df_clean["win"].sum()
    clean_pnl = df_clean["pnl"].sum()
    
    print("=" * 60)
    print("YESTERDAY (MAY 22) - OPTIMIZED SCHEDULE ANALYSIS")
    print("=" * 60)
    print(f"Strategy: Streak-2 (Live Bot Logic)")
    print(f"Toxic Hours Skipped: {TOXIC_HOURS}")
    print("-" * 60)
    
    print(f"RAW PERFORMANCE (All 24 Hours):")
    print(f"  • Total Trades: {total_trades}")
    print(f"  • Win Rate:     {(total_win/total_trades)*100:.1f}%")
    print(f"  • Net PnL:      ${total_pnl:.2f}")
    
    print(f"\nCLEAN PERFORMANCE (Only Safe Hours):")
    print(f"  • Total Trades: {clean_trades}")
    if clean_trades > 0:
        print(f"  • Win Rate:     {(clean_win/clean_trades)*100:.1f}%")
        print(f"  • Net PnL:      ${clean_pnl:+.2f}")
        
        improvement = clean_pnl - total_pnl
        print(f"\nRESULT: You saved ${improvement:.2f} by stopping the bot in toxic hours.")
        
        # Best Asset Breakdown in Clean Hours
        print("\nClean Window Asset Breakdown:")
        for (sym, win_size), group in df_clean.groupby(["symbol", "window"]):
            g_trades = len(group)
            g_wins = group["win"].sum()
            g_pnl = group["pnl"].sum()
            print(f"  • {sym} {win_size}m: {g_trades} trades, {(g_wins/g_trades)*100:.1f}% Win, ${g_pnl:+.2f} PnL")
    else:
        print("  • No trades occurred in the safe hours.")
    print("=" * 60)

if __name__ == "__main__":
    calculate_clean_yesterday()
