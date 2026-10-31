import sqlite3
import re
from datetime import datetime
from collections import defaultdict

DB_PATH = 'data/reversebot_rd.db'

def extract_window(question):
    # Extracts "May 22, 11:35AM-11:40AM ET" from the full question
    match = re.search(r' - (.*)$', question)
    return match.group(1) if match else question

def analyze_correlations():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = """
            SELECT 
                p.position_id, p.opened_at, p.realised_pnl, p.side,
                m.question
            FROM paper_positions p
            JOIN markets m ON p.market_id = m.market_id
            WHERE p.realised_pnl IS NOT NULL
            ORDER BY p.opened_at ASC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"Error reading database: {e}")
        return

    # Group by window
    windows = defaultdict(list)
    for r in rows:
        t = dict(r)
        win_str = extract_window(t['question'])
        windows[win_str].append(t)

    actual_total_pnl = 0.0
    hypothetical_pnl = 0.0
    redundant_pnl_avoided = 0.0
    correlation_events = 0
    
    print("=" * 80)
    print(f"{'MARKET WINDOW':<40} | {'FIRST ASSET':<10} | {'LATE ASSET':<10} | {'AVOIDED PNL'}")
    print("-" * 80)

    for win_str, trades in windows.items():
        # Check for correlated pair (BTC and ETH)
        btc_trades = [t for t in trades if "BTC" in t['question'] or "Bitcoin" in t['question']]
        eth_trades = [t for t in trades if "ETH" in t['question'] or "Ethereum" in t['question']]
        
        # We only care about windows where BOTH were traded
        if btc_trades and eth_trades:
            correlation_events += 1
            # Sort all trades in this window by opened_at
            sorted_trades = sorted(trades, key=lambda x: x['opened_at'])
            
            first_trade = sorted_trades[0]
            later_trades = sorted_trades[1:]
            
            # Identify assets for the report
            first_asset = "BTC" if "BTC" in first_trade['question'] or "Bitcoin" in first_trade['question'] else "ETH"
            
            for lt in later_trades:
                late_asset = "BTC" if "BTC" in lt['question'] or "Bitcoin" in lt['question'] else "ETH"
                late_pnl = lt['realised_pnl'] or 0.0
                redundant_pnl_avoided += late_pnl
                
                print(f"{win_str[:39]:<40} | {first_asset:<10} | {late_asset:<10} | {late_pnl:+.2f}")

    # Calculate Totals
    for r in rows:
        actual_total_pnl += (r['realised_pnl'] or 0.0)

    # Hypothetical is Total minus the ones we "avoided"
    hypothetical_pnl = actual_total_pnl - redundant_pnl_avoided

    print("-" * 80)
    print(f"\nSUMMARY OF CORRELATION IMPACT:")
    print(f"  • Correlated Windows Traded:    {correlation_events}")
    print(f"  • Actual Net PnL:               ${actual_total_pnl:.2f}")
    print(f"  • Hypothetical PnL (First Only): ${hypothetical_pnl:.2f}")
    
    diff = hypothetical_pnl - actual_total_pnl
    if diff > 0:
        print(f"  • RESULT: Trading both assets COST you ${abs(diff):.2f}. (You would be more profitable trading only one).")
    else:
        print(f"  • RESULT: Trading both assets EARNED you ${abs(diff):.2f} extra. (The correlation actually worked in your favor).")

if __name__ == "__main__":
    analyze_correlations()
