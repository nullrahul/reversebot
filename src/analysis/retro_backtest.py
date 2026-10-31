import os
import sqlite3
import sys
from datetime import datetime, timezone

def calculate_metrics(pnls):
    if not pnls:
        return {
            "count": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0
        }
    
    count = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = (wins / count) * 100.0 if count > 0 else 0.0
    total_pnl = sum(pnls)
    
    # Cumulative PnL series
    cum_pnl = []
    curr = 0.0
    for p in pnls:
        curr += p
        cum_pnl.append(curr)
        
    # Peak-to-trough drawdown calculation
    max_dd = 0.0
    peak = 0.0
    for val in cum_pnl:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
            
    return {
        "count": count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "max_drawdown": max_dd
    }

def main():
    # Database path
    db_path = "data/reversebot_rd.db"
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at {db_path}", file=sys.stderr)
        sys.exit(1)
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 1. Fetch trades for stop_loss (opened_at > 1718260755)
    # 2. Fetch trades for reversal_stop_loss (All-Time)
    # Only clean trades: seconds_to_close_at_entry >= 0 and order_status = 'FILLED'
    
    queries = {
        "stop_loss": """
            SELECT realised_pnl, opened_at FROM profile_positions
            WHERE profile_name = 'stop_loss'
              AND opened_at > 1718260755
              AND seconds_to_close_at_entry >= 0
              AND order_status = 'FILLED'
              AND realised_pnl IS NOT NULL
            ORDER BY opened_at ASC
        """,
        "reversal_stop_loss": """
            SELECT realised_pnl, opened_at FROM profile_positions
            WHERE profile_name = 'reversal_stop_loss'
              AND seconds_to_close_at_entry >= 0
              AND order_status = 'FILLED'
              AND realised_pnl IS NOT NULL
            ORDER BY opened_at ASC
        """
    }
    
    results = {}
    
    for profile, query in queries.items():
        cursor.execute(query)
        rows = cursor.fetchall()
        
        orig_pnls = []
        sim_pnls = []
        
        for row in rows:
            pnl = row["realised_pnl"]
            opened_at = row["opened_at"]
            
            # UTC time conversion
            dt = datetime.fromtimestamp(opened_at, timezone.utc)
            utc_hour = dt.hour
            utc_day = dt.weekday() # 4=Friday, 5=Saturday, 6=Sunday
            
            dropped = False
            scaled_pnl = pnl
            
            if profile == "stop_loss":
                # Time filter: UTC 20:00 to 02:00 (20, 21, 22, 23, 0, 1)
                if utc_hour >= 20 or utc_hour <= 1:
                    dropped = True
                # Weekend scaling: 0.75x
                elif utc_day in (4, 5, 6):
                    scaled_pnl = pnl * 0.75
                    
            elif profile == "reversal_stop_loss":
                # Time filter: UTC 08:00 to 12:00 (8, 9, 10, 11)
                if 8 <= utc_hour <= 11:
                    dropped = True
                # Weekend scaling: 1.25x
                elif utc_day in (4, 5, 6):
                    scaled_pnl = pnl * 1.25
            
            orig_pnls.append(pnl)
            if not dropped:
                sim_pnls.append(scaled_pnl)
                
        orig_metrics = calculate_metrics(orig_pnls)
        sim_metrics = calculate_metrics(sim_pnls)
        
        results[profile] = {
            "original": orig_metrics,
            "simulated": sim_metrics
        }
        
    conn.close()
    
    # Output markdown comparison
    print("# Retroactive Counterfactual Backtest Results\n")
    for profile, data in results.items():
        orig = data["original"]
        sim = data["simulated"]
        
        print(f"## Profile: `{profile}`")
        print("| Metric | Original Data | Simulated (Guardian Rules) | Impact / Diff |")
        print("| :--- | :--- | :--- | :--- |")
        print(f"| **Trade Count** | {orig['count']} | {sim['count']} | {sim['count'] - orig['count']} |")
        print(f"| **Win Rate** | {orig['win_rate']:.2f}% | {sim['win_rate']:.2f}% | {sim['win_rate'] - orig['win_rate']:.2f}% |")
        print(f"| **Total P&L** | {orig['total_pnl']:.4f} USDC | {sim['total_pnl']:.4f} USDC | {sim['total_pnl'] - orig['total_pnl']:.4f} USDC |")
        print(f"| **Max Drawdown** | {orig['max_drawdown']:.4f} USD | {sim['max_drawdown']:.4f} USD | {sim['max_drawdown'] - orig['max_drawdown']:.4f} USD |")
        print()

if __name__ == "__main__":
    main()
