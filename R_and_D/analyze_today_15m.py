#!/usr/bin/env python3
"""
RuFlo V3 R&D Sandbox: 15-Minute Window Trades Auditor for May 28, 2026
"""

import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def run_analysis():
    db_path = os.path.expanduser("~/reversebot/data/reversebot_rd.db")
    if not os.path.exists(db_path):
        # Local fallback if run locally
        db_path = "data/reversebot_rd.db"
        if not os.path.exists(db_path):
            print(f"Error: Database not found at {db_path}")
            return
            
    print(f"Analyzing SQLite database: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    et_tz = ZoneInfo("America/New_York")
    target_date = "2026-05-28"
    
    for table in ["positions", "paper_positions"]:
        print("\n" + "=" * 100)
        print(f" 📊 TABLE: {table.upper()}")
        print("=" * 100)
        
        cursor = conn.cursor()
        query = f"""
            SELECT position_id, market_id, symbol, window_minutes, side, entry_price, exit_price,
                   size_usdc, realised_pnl, opened_at, closed_at, order_status, close_reason
            FROM {table}
            WHERE window_minutes = 15
        """
        try:
            cursor.execute(query)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            print(f"Error querying table {table}: {e}")
            continue
            
        trades_today = []
        for r in rows:
            opened_dt = datetime.fromtimestamp(r["opened_at"], tz=timezone.utc).astimezone(et_tz) if r["opened_at"] else None
            closed_dt = datetime.fromtimestamp(r["closed_at"], tz=timezone.utc).astimezone(et_tz) if r["closed_at"] else None
            
            is_today = False
            if opened_dt and opened_dt.strftime("%Y-%m-%d") == target_date:
                is_today = True
            if closed_dt and closed_dt.strftime("%Y-%m-%d") == target_date:
                is_today = True
                
            if is_today:
                trades_today.append((r, opened_dt, closed_dt))
                
        print(f"Total 15m trades closed or opened on {target_date} (ET): {len(trades_today)}")
        if not trades_today:
            continue
            
        # Aggregate statistics
        total_trades = len(trades_today)
        wins = 0
        losses = 0
        pending = 0
        total_pnl = 0.0
        
        for r, op_dt, cl_dt in trades_today:
            pnl = r["realised_pnl"]
            status = r["order_status"]
            
            if status in ["OPEN", "PENDING"] or pnl is None:
                pending += 1
            else:
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                    
        print(f"  • Total Trades: {total_trades}")
        print(f"  • Wins: {wins}")
        print(f"  • Losses: {losses}")
        print(f"  • Pending/Open: {pending}")
        print(f"  • Net Realized P&L: ${total_pnl:.4f}")
        print()
        
        print(f"{'Symbol':<6} | {'Side':<4} | {'Entry':<6} | {'Exit':<6} | {'Size':<6} | {'P&L':<8} | {'Opened (ET)':<11} | {'Closed (ET)':<11} | {'Status':<8} | {'Reason':<10}")
        print("-" * 100)
        for r, op_dt, cl_dt in sorted(trades_today, key=lambda x: x[1] if x[1] else datetime.min):
            op_str = op_dt.strftime("%I:%M %p") if op_dt else "N/A"
            cl_str = cl_dt.strftime("%I:%M %p") if cl_dt else "N/A"
            pnl_val = f"${r['realised_pnl']:.4f}" if r['realised_pnl'] is not None else "N/A"
            entry_val = r['entry_price'] if r['entry_price'] else 0.0
            exit_val = r['exit_price'] if r['exit_price'] else 0.0
            print(f"{r['symbol']:<6} | {r['side']:<4} | {entry_val:<6.4f} | {exit_val:<6.4f} | {r['size_usdc']:<6.2f} | {pnl_val:<8} | {op_str:<11} | {cl_str:<11} | {r['order_status']:<8} | {r['close_reason'] or 'N/A':<10}")
        print("-" * 100)
        
    conn.close()

if __name__ == "__main__":
    run_analysis()
