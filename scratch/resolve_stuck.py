import sqlite3
import requests
import json
import time
from datetime import datetime

DB_PATH = "/home/ubuntu/reversebot/data/reversebot_rd.db"
GAMMA_URL = "https://gamma-api.polymarket.com/markets/"

def get_realised_pnl(exit_price, shares, size_usdc):
    # Standard P&L calculation
    return (exit_price * shares) - size_usdc

def resolve_stuck():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get open paper positions
    cur.execute("SELECT * FROM paper_positions WHERE order_status IN ('OPEN', 'PENDING')")
    positions = cur.fetchall()

    if not positions:
        print("No stuck positions found.")
        return

    print(f"Checking {len(positions)} positions...")

    for pos in positions:
        pid = pos['position_id']
        mid = pos['market_id']
        shares = pos['shares_bought']
        size_usdc = pos['size_usdc']
        
        print(f"Checking Market {mid} for Position {pid}...")
        
        try:
            resp = requests.get(f"{GAMMA_URL}{mid}")
            if resp.status_code != 200:
                print(f"  Failed to fetch market data: {resp.status_code}")
                continue
            
            data = resp.json()
            prices_raw = data.get("outcomePrices")
            if not prices_raw:
                print("  No prices found.")
                continue
            
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            
            if len(prices) != 2:
                print(f"  Unexpected prices length: {len(prices)}")
                continue
            
            # Polymarket outcome prices are strings like "0" and "1" when resolved
            p_yes = float(prices[0])
            p_no = float(prices[1])
            
            # If one of them is 1.0, it's resolved
            if p_yes == 1.0 or p_no == 1.0 or p_yes == 0.0 or p_no == 0.0:
                if p_yes + p_no != 1.0:
                    # Might still be in transition or weird state
                    if p_yes == 0.0 and p_no == 0.0:
                        print("  Market still zeroed out.")
                        continue
                
                # Exit price for "NO" side is p_no
                exit_price = p_no
                pnl = get_realised_pnl(exit_price, shares, size_usdc)
                
                print(f"  RESOLVED! Outcome: {'YES' if p_yes == 1.0 else 'NO'}. P&L: ${pnl:.4f}")
                
                # Close in DB
                cur.execute("""
                    UPDATE paper_positions 
                    SET order_status = 'FILLED',
                        closed_at = ?,
                        exit_price = ?,
                        realised_pnl = ?,
                        close_reason = 'RESOLUTION'
                    WHERE position_id = ?
                """, (int(time.time()), exit_price, pnl, pid))
                conn.commit()
            else:
                print(f"  Market still active. Prices: Yes={p_yes}, No={p_no}")

        except Exception as e:
            print(f"  Error: {e}")

    conn.close()

if __name__ == "__main__":
    resolve_stuck()
