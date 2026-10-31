#!/usr/bin/env python3
"""
RuFlo V3 R&D Sandbox: Gamma API Micro-Markets Resolution Fetcher
Directly queries Polymarket's Gamma API for historical closed markets,
filters for last week (Monday, May 18 to Sunday, May 24, 2026 UTC),
translates timestamps to Eastern Time (ET) with day names, and displays/saves them.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Last week boundaries in UTC:
# Monday, May 18, 2026 00:00:00 UTC = 1779062400
# Sunday, May 24, 2026 23:59:59 UTC = 1779667200
START_UTC = 1779062400
END_UTC = 1779667200

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

def fetch_closed_markets(search_query="updown", limit=100):
    """
    Paginates through Gamma API /markets to fetch all closed micro-markets.
    """
    all_markets = []
    offset = 0
    
    print("Connecting to Polymarket Gamma API...")
    
    while True:
        params = {
            "closed": "true",
            "active": "false",
            "limit": limit,
            "offset": offset,
            "search": search_query
        }
        
        url_params = urllib.parse.urlencode(params)
        url = f"{GAMMA_API_URL}?{url_params}"
        
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req) as response:
                if response.status != 200:
                    print(f"Error fetching data: HTTP {response.status}")
                    break
                data = json.loads(response.read().decode('utf-8'))
                
            if not data or not isinstance(data, list):
                break
                
            all_markets.extend(data)
            print(f"  Fetched {len(data)} closed markets (Total so far: {len(all_markets)})...")
            
            if len(data) < limit:
                # Reached the end of pagination
                break
                
            offset += limit
            
        except Exception as e:
            print(f"Network error: {e}")
            break
            
    return all_markets

def parse_and_filter_markets(raw_markets):
    """
    Parses and filters markets for BTC/ETH 5m/15m within the target week.
    """
    filtered = []
    
    for raw in raw_markets:
        # Extract title and slug
        slug = str(raw.get("slug", "")).lower()
        question = str(raw.get("question", "")).lower()
        
        # We only want crypto short-window "up or down" micro-markets
        if not ("updown" in slug or "up or down" in question):
            continue
            
        # Determine symbol
        if "btc" in slug or "bitcoin" in question:
            symbol = "BTC"
        elif "eth" in slug or "ethereum" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question:
            symbol = "SOL"
        elif "doge" in slug or "dogecoin" in question:
            symbol = "DOGE"
        else:
            continue
            
        # Determine window size (5m vs 15m)
        if "-5m-" in slug or "5-minute" in question:
            window = 5
        elif "-15m-" in slug or "15-minute" in question:
            window = 15
        else:
            continue
            
        # Parse close timestamp (endDate)
        end_date_str = raw.get("endDate") or raw.get("end_date_iso")
        if not end_date_str:
            continue
            
        try:
            # ISO 8601 parsing (e.g. 2026-05-24T18:45:00Z)
            dt_utc = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            closes_at = int(dt_utc.timestamp())
        except Exception:
            continue
            
        # Filter for the target week (May 18 to May 24, 2026 UTC)
        if not (START_UTC <= closes_at <= END_UTC):
            continue
            
        # Parse final outcome resolutions
        # Polymarket closed markets store final prices in outcomePrices (e.g. ["1", "0"])
        # and outcomes labels in outcomes (e.g. ["YES", "NO"])
        raw_outcomes = raw.get("outcomes", [])
        if isinstance(raw_outcomes, str):
            try: raw_outcomes = json.loads(raw_outcomes)
            except: raw_outcomes = []
            
        raw_prices = raw.get("outcomePrices", [])
        if isinstance(raw_prices, str):
            try: raw_prices = json.loads(raw_prices)
            except: raw_prices = []
            
        if len(raw_outcomes) < 2 or len(raw_prices) < 2:
            continue
            
        try:
            yes_final = float(raw_prices[0])
            no_final = float(raw_prices[1])
        except (ValueError, TypeError):
            continue
            
        # YES = UP, NO = DOWN
        if yes_final > 0.9:
            resolution = "UP"
        elif no_final > 0.9:
            resolution = "DOWN"
        else:
            resolution = "UNKNOWN"
            
        filtered.append({
            "market_id": raw.get("id"),
            "symbol": symbol,
            "window_minutes": window,
            "closes_at": closes_at,
            "resolution": resolution,
            "question": raw.get("question"),
            "slug": slug,
            "yes_price": yes_final,
            "no_price": no_final
        })
        
    return filtered

def print_bucket(title, rows):
    print("=" * 105)
    print(f" 📅 {title.upper()} (Total: {len(rows)})")
    print("=" * 105)
    print(f"{'#':<4} | {'Asset':<5} | {'Day':<10} | {'Date (ET)':<10} | {'Time (ET)':<11} | {'Resolution':<10} | {'Market ID':<35}")
    print("-" * 105)
    
    # Sort chronologically
    sorted_rows = sorted(rows, key=lambda x: x["closes_at"])
    
    for idx, r in enumerate(sorted_rows, 1):
        closes_at = r["closes_at"]
        dt_utc = datetime.fromtimestamp(closes_at, tz=timezone.utc)
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        
        day_name = dt_et.strftime("%A")
        date_str = dt_et.strftime("%Y-%m-%d")
        time_str = dt_et.strftime("%I:%M %p")
            
        resolution = r["resolution"]
        color_res = f"\033[92m{resolution:<10}\033[0m" if resolution == "UP" else f"\033[93m{resolution:<10}\033[0m"
        
        print(f"{idx:<4} | {r['symbol']:<5} | {day_name:<10} | {date_str:<10} | {time_str:<11} | {color_res} | {r['market_id']:<35}")
    print("-" * 105)
    print()

def save_report(filtered_markets):
    """
    Saves the fetched resolved markets to R_and_D/backtesting/
    as both a raw JSON dump and a formatted markdown checklist.
    """
    os.makedirs("R_and_D/backtesting", exist_ok=True)
    
    # 1. Save JSON dump
    json_path = "R_and_D/backtesting/may18_may24_polymarket_resolutions.json"
    with open(json_path, "w") as f:
        json.dump(filtered_markets, f, indent=4)
        
    # 2. Save Markdown report
    md_path = "R_and_D/backtesting/may18_may24_polymarket_resolutions.md"
    
    rows_5m = sorted([r for r in filtered_markets if r["window_minutes"] == 5], key=lambda x: x["closes_at"])
    rows_15m = sorted([r for r in filtered_markets if r["window_minutes"] == 15], key=lambda x: x["closes_at"])
    
    with open(md_path, "w") as f:
        f.write("# 🕯️ Polymarket Micro-Markets Resolutions Report (May 18 - May 24, 2026)\n\n")
        f.write(f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        f.write(f"Target Period: **Monday, May 18 00:00 UTC** to **Sunday, May 24 23:59 UTC**\n\n")
        f.write(f"- **Total Markets Retrieved**: {len(filtered_markets)}\n")
        f.write(f"- **5-Minute timeframes**: {len(rows_5m)}\n")
        f.write(f"- **15-Minute timeframes**: {len(rows_15m)}\n\n")
        
        # Write 5m
        f.write("## 📅 5-Minute Window (5m) Resolutions\n\n")
        f.write("| # | Asset | Day (ET) | Date (ET) | Time (ET) | Resolution | Market ID | Question |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for idx, r in enumerate(rows_5m, 1):
            dt_et = datetime.fromtimestamp(r["closes_at"], tz=timezone.utc).astimezone(ZoneInfo("America/New_York"))
            f.write(f"| {idx} | {r['symbol']} | {dt_et.strftime('%A')} | {dt_et.strftime('%Y-%m-%d')} | {dt_et.strftime('%I:%M %p')} | **{r['resolution']}** | `{r['market_id']}` | {r['question']} |\n")
            
        f.write("\n")
        
        # Write 15m
        f.write("## 📅 15-Minute Window (15m) Resolutions\n\n")
        f.write("| # | Asset | Day (ET) | Date (ET) | Time (ET) | Resolution | Market ID | Question |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for idx, r in enumerate(rows_15m, 1):
            dt_et = datetime.fromtimestamp(r["closes_at"], tz=timezone.utc).astimezone(ZoneInfo("America/New_York"))
            f.write(f"| {idx} | {r['symbol']} | {dt_et.strftime('%A')} | {dt_et.strftime('%Y-%m-%d')} | {dt_et.strftime('%I:%M %p')} | **{r['resolution']}** | `{r['market_id']}` | {r['question']} |\n")
            
    print(f"\033[92mSaved raw JSON data to: {json_path}\033[0m")
    print(f"\033[92mSaved formatted Markdown report to: {md_path}\033[0m")

def main():
    print("\033[95m" + "=" * 80 + "\033[0m")
    print("\033[95m ♊ Polymarket Gamma API — Historical Micro-Markets Resolutions Fetcher \033[0m")
    print("\033[95m" + "=" * 80 + "\033[0m")
    print("Goal: Fetch Ground-Truth resolutions directly from Polymarket Gamma API")
    print("Target Period: Last Week (Monday, May 18 to Sunday, May 24, 2026 UTC)")
    print()
    
    raw_markets = fetch_closed_markets()
    if not raw_markets:
        print("\033[91mError: No markets returned from the Gamma API.\033[0m")
        return
        
    filtered = parse_and_filter_markets(raw_markets)
    
    if not filtered:
        print("\033[93mNo short-window updown micro-markets found for the target week in the fetched results.\033[0m")
        return
        
    # Group by timeframe window size (5m vs 15m)
    rows_5m = [r for r in filtered if r["window_minutes"] == 5]
    rows_15m = [r for r in filtered if r["window_minutes"] == 15]
    
    print(f"\nSuccessfully matched {len(filtered)} ground-truth resolved micro-markets for last week!")
    print(f"  - 5-Minute Window (5m):  {len(rows_5m)}")
    print(f"  - 15-Minute Window (15m): {len(rows_15m)}")
    print()
    
    if rows_5m:
        print_bucket("5-Minute Window (5m) Ground-Truth Resolutions", rows_5m)
        
    if rows_15m:
        print_bucket("15-Minute Window (15m) Ground-Truth Resolutions", rows_15m)
        
    # Save results to R_and_D/backtesting/
    save_report(filtered)

if __name__ == "__main__":
    main()
