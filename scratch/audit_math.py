import sqlite3
import json

def audit_recent():
    conn = sqlite3.connect("/home/ubuntu/reversebot/reversebot_rd.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT p.position_id, p.entry_price, p.exit_price, p.realised_pnl, p.close_reason, p.notes, p.size_usdc 
        FROM paper_positions p
        WHERE p.order_status = 'FILLED' 
        ORDER BY p.closed_at DESC 
        LIMIT 10
    """).fetchall()
    
    print(f"{'ID':<10} | {'P&L':<8} | {'Reason':<12} | {'Notes Status'}")
    print("-" * 60)
    for r in rows:
        notes = r['notes']
        status = "OLD_RAW"
        if notes and notes.startswith("{"):
            status = "NEW_JSON"
        
        print(f"{r['position_id'][:8]:<10} | ${r['realised_pnl']:>7.3f} | {r['close_reason']:<12} | {status}")

if __name__ == "__main__":
    audit_recent()
