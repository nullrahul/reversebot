#!/usr/bin/env python3
"""
RuFlo V3 R&D Sandbox: Resolved Markets Auditor
Queries the database for the past 500 resolved micro-markets, converts their close times
into Eastern Time (ET) with full date, day names, and prints them separately for 5m and 15m.
"""

import os
import sys
import sqlite3
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

# Default paths for Local and VPS
DEFAULT_LOCAL_DB = os.path.expanduser("~/Documents/reversebot/data/reversebot_rd.db")
DEFAULT_VPS_DB = os.path.expanduser("~/reversebot/data/reversebot_rd.db")

def parse_args():
    parser = argparse.ArgumentParser(description="Query and print resolved markets in ET time.")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to the SQLite database file. Defaults to auto-detect."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Number of resolved markets to retrieve (default: 500)."
    )
    return parser.parse_args()

def get_db_path(custom_path):
    if custom_path:
        return custom_path
    
    # Auto-detect local vs VPS
    if os.path.exists(DEFAULT_LOCAL_DB):
        return DEFAULT_LOCAL_DB
    elif os.path.exists(DEFAULT_VPS_DB):
        return DEFAULT_VPS_DB
    
    # Fallback to relative path in local workspace
    workspace_db = "data/reversebot_rd.db"
    if os.path.exists(workspace_db):
        return workspace_db
        
    return DEFAULT_LOCAL_DB

def print_bucket(title, rows):
    print("=" * 105)
    print(f" 📅 {title.upper()} (Total: {len(rows)})")
    print("=" * 105)
    print(f"{'#':<4} | {'Asset':<5} | {'Day':<10} | {'Date (ET)':<10} | {'Time (ET)':<11} | {'Resolution':<10} | {'Market ID':<35}")
    print("-" * 105)
    
    for idx, r in enumerate(rows, 1):
        raw_ts = r["signal_timestamp"]
        # Parse ISO UTC timestamp
        try:
            # Replace Z with UTC offset to parse correctly
            clean_ts = raw_ts.replace("Z", "+00:00")
            dt_utc = datetime.fromisoformat(clean_ts)
            dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
            
            day_name = dt_et.strftime("%A")
            date_str = dt_et.strftime("%Y-%m-%d")
            time_str = dt_et.strftime("%I:%M %p")
        except Exception:
            day_name = "Unknown"
            date_str = "Unknown"
            time_str = raw_ts[:16]
            
        resolution = r["eventual_resolution"]
        # Highlight UP in Green and DOWN in Red/Orange for clear visual auditing
        color_res = f"\033[92m{resolution:<10}\033[0m" if resolution == "UP" else f"\033[93m{resolution:<10}\033[0m"
        
        print(f"{idx:<4} | {r['symbol']:<5} | {day_name:<10} | {date_str:<10} | {time_str:<11} | {color_res} | {r['market_id']:<35}")
    print("-" * 105)
    print()

def main():
    args = parse_args()
    db_path = get_db_path(args.db)
    
    print("\033[95m" + "=" * 80 + "\033[0m")
    print("\033[95m ♊ Polymarket ReverseBot (RuFlo V3) — Resolved Markets Auditor \033[0m")
    print("\033[95m" + "=" * 80 + "\033[0m")
    print(f"Target Database: {db_path}")
    
    if not os.path.exists(db_path):
        print(f"\033[91mError: Database file not found at {db_path}\033[0m")
        print("\nHow to copy production data from VPS to Local:")
        print("  Run this command on your local Mac terminal:")
        print("  rsync -avz reversebot-vps:~/reversebot/data/reversebot_rd.db data/reversebot_rd.db")
        sys.exit(1)
        
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Query distinct resolved markets from signals table
        query = """
            SELECT DISTINCT market_id, symbol, window_size, eventual_resolution, signal_timestamp
            FROM signals
            WHERE eventual_resolution IS NOT NULL AND eventual_resolution != 'UNKNOWN'
            ORDER BY signal_timestamp DESC
            LIMIT ?
        """
        cursor.execute(query, (args.limit,))
        rows = cursor.fetchall()
        
        if not rows:
            print("\nNo resolved markets found in the signals table yet.")
            conn.close()
            return
            
        # Group by timeframe window size (5m vs 15m)
        rows_5m = [r for r in rows if str(r["window_size"]) == "5"]
        rows_15m = [r for r in rows if str(r["window_size"]) == "15"]
        
        print(f"Successfully retrieved {len(rows)} resolved markets.")
        print(f"  - 5-Minute Window (5m):  {len(rows_5m)}")
        print(f"  - 15-Minute Window (15m): {len(rows_15m)}")
        print()
        
        if rows_5m:
            print_bucket("5-Minute Window (5m) Resolved Markets", rows_5m)
            
        if rows_15m:
            print_bucket("15-Minute Window (15m) Resolved Markets", rows_15m)
            
        conn.close()
        
    except Exception as e:
        print(f"\033[91mDatabase query error: {str(e)}\033[0m")
        sys.exit(1)

if __name__ == "__main__":
    main()
