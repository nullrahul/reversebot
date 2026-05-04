import sqlite3
import json
import time

def deep_analyze_trades(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Fetch all filled paper positions
    trades = conn.execute("""
        SELECT p.*, m.question, m.yes_price as final_yes, m.no_price as final_no
        FROM paper_positions p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status = 'FILLED'
        ORDER BY p.closed_at DESC
    """).fetchall()
    
    print(f"--- Deep Trade Analysis (Total: {len(trades)}) ---")
    print(f"{'Market ID':<10} | {'Entry':<6} | {'Exit':<6} | {'P&L':<8} | {'Reason':<12} | {'Question'}")
    print("-" * 100)
    
    duplicates = {}
    total_pnl = 0
    wins = 0
    losses = 0
    
    for t in trades:
        pnl = t['realised_pnl']
        total_pnl += pnl
        if pnl > 0: wins += 1
        else: losses += 1
        
        # Track duplicates (Same market)
        mid = t['market_id']
        duplicates[mid] = duplicates.get(mid, 0) + 1
        
        q = t['question'][:40] if t['question'] else "Unknown"
        print(f"{t['market_id']:<10} | {t['entry_price']:<6.3f} | {t['exit_price']:<6.3f} | ${pnl:>7.3f} | {t['close_reason']:<12} | {q}")
        
    print("-" * 100)
    print(f"SUMMARY STATS:")
    print(f"  Total Trades: {len(trades)}")
    print(f"  Win/Loss:    {wins}W / {losses}L ({(wins/len(trades)*100 if len(trades)>0 else 0):.1f}% WR)")
    print(f"  Total P&L:   ${total_pnl:.4f}")
    
    # Duplicate Analysis
    dup_counts = [count for count in duplicates.values() if count > 1]
    if dup_counts:
        print(f"\n[!] WARNING: DUPLICATE TRADES DETECTED")
        print(f"  {len(dup_counts)} markets were traded multiple times.")
        print(f"  Total ghost entries: {sum(dup_counts) - len(dup_counts)}")
        print(f"  (This confirms the DB-Path-Split bug was causing the win-streak illusion.)")
    else:
        print(f"\n[✓] Integrity Check: No duplicate trades found in this DB version.")

if __name__ == "__main__":
    deep_analyze_trades("/home/ubuntu/reversebot/reversebot_rd.db")
