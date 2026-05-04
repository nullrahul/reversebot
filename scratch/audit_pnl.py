import sqlite3
import json

def audit_pnl(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    print(f"--- P&L Audit Report: {db_path} ---")
    
    # Check paper_positions
    total_cost = conn.execute("SELECT SUM(size_usdc) FROM paper_positions").fetchone()[0] or 0
    total_realised = conn.execute("SELECT SUM(realised_pnl) FROM paper_positions WHERE order_status='FILLED'").fetchone()[0] or 0
    open_exposure = conn.execute("SELECT SUM(size_usdc) FROM paper_positions WHERE order_status='OPEN'").fetchone()[0] or 0
    
    print(f"Total risked: ${total_cost:.2f}")
    print(f"Total realised: ${total_realised:.2f}")
    print(f"Open exposure: ${open_exposure:.2f}")
    
    print("\n--- Recent Position States ---")
    rows = conn.execute("SELECT position_id, order_status, size_usdc, entry_price, realised_pnl, close_reason FROM paper_positions ORDER BY opened_at DESC LIMIT 15").fetchall()
    for r in rows:
        pnl_str = f"${r['realised_pnl']:.2f}" if r['realised_pnl'] is not None else "N/A"
        print(f"ID: {r['position_id'][:8]} | {r['order_status']:8} | Size: ${r['size_usdc']:5.2f} | PnL: {pnl_str:6} | Reason: {r['close_reason']}")

    # Check for anomalies: positions that are OPEN but market has passed
    import time
    now = int(time.time())
    anomalies = conn.execute("SELECT p.position_id, p.market_id FROM paper_positions p JOIN markets m ON p.market_id = m.market_id WHERE p.order_status='OPEN' AND m.closes_at < ?", (now,)).fetchall()
    if anomalies:
        print(f"\n[!] Found {len(anomalies)} ghost open positions (Market closed but position still OPEN)")
        for a in anomalies:
            print(f"  - {a[0][:8]} (Market: {a[1]})")

if __name__ == "__main__":
    import os
    db_paths = ["/home/ubuntu/reversebot/reversebot_rd.db", "/home/ubuntu/reversebot/reversebot_dump.db"]
    for path in db_paths:
        if os.path.exists(path):
            audit_pnl(path)
