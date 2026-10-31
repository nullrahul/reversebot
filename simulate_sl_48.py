import sqlite3
import datetime

DB_PATH = 'data/reversebot_rd.db'

def simulate_48_sl():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get start of May 22, 2026 UTC
    dt_start = datetime.datetime(2026, 5, 22, 0, 0, 0, tzinfo=datetime.timezone.utc)
    ts_start = int(dt_start.timestamp())
    
    query = """
        SELECT 
            p.position_id, p.side, p.entry_price, p.size_usdc, p.realised_pnl, 
            p.max_price_seen, p.min_price_seen, p.opened_at, m.question
        FROM paper_positions p
        JOIN markets m ON p.market_id = m.market_id
        WHERE p.opened_at >= ? 
          AND p.max_price_seen IS NOT NULL 
          AND p.min_price_seen IS NOT NULL
          AND p.realised_pnl IS NOT NULL
    """
    cursor.execute(query, (ts_start,))
    rows = cursor.fetchall()
    
    if not rows:
        print("No trades found to simulate.")
        return

    actual_pnl = 0.0
    hypothetical_pnl = 0.0
    
    actual_wins = 0
    actual_losses = 0
    
    hypo_wins = 0
    hypo_losses = 0
    
    SL_THRESHOLD = 48.0 # 48% drop

    for r in rows:
        t = dict(r)
        entry = t['entry_price']
        min_p = t['min_price_seen']
        size = t['size_usdc']
        real_pnl = t['realised_pnl']
        
        if entry <= 0: continue
            
        dip_pct = ((entry - min_p) / entry) * 100
        
        # Track actual performance
        actual_pnl += real_pnl
        if real_pnl > 0:
            actual_wins += 1
        else:
            actual_losses += 1
            
        # Simulate 48% SL
        if real_pnl > 0:
            # It was a winner. Did the dip trigger our SL before it won?
            if dip_pct >= SL_THRESHOLD:
                # Killed a winner. It stops out for a 48% loss of position size
                sl_loss = -(size * (SL_THRESHOLD / 100.0))
                hypothetical_pnl += sl_loss
                hypo_losses += 1
            else:
                # Winner survives the dip
                hypothetical_pnl += real_pnl
                hypo_wins += 1
        else:
            # It was a loser. 
            if dip_pct >= SL_THRESHOLD:
                # Saved some money by cutting early at 48% instead of the actual loss (which is usually ~100% or close to it)
                sl_loss = -(size * (SL_THRESHOLD / 100.0))
                
                # We can't lose *more* than the actual loss if the actual loss was somehow less than 48%.
                # So we take the max of (sl_loss, real_pnl) 
                hypo_pnl = max(sl_loss, real_pnl)
                
                hypothetical_pnl += hypo_pnl
                hypo_losses += 1
            else:
                # Never hit 48% SL, so it just takes the normal loss
                hypothetical_pnl += real_pnl
                hypo_losses += 1

    print("=" * 60)
    print("HYPOTHETICAL RESULTS: STRICT 48% STOP LOSS")
    print("=" * 60)
    
    print(f"[ACTUAL PERFORMANCE (No SL)]")
    print(f"  • Total Trades: {actual_wins + actual_losses}")
    print(f"  • Wins / Losses: {actual_wins} / {actual_losses} ({(actual_wins/(actual_wins+actual_losses))*100:.1f}%)")
    print(f"  • Net PnL: ${actual_pnl:.2f}")
    
    print(f"\n[HYPOTHETICAL PERFORMANCE (With -48% SL)]")
    print(f"  • Total Trades: {hypo_wins + hypo_losses}")
    print(f"  • Wins / Losses: {hypo_wins} / {hypo_losses} ({(hypo_wins/(hypo_wins+hypo_losses))*100:.1f}%)")
    print(f"  • Net PnL: ${hypothetical_pnl:.2f}")
    
    difference = hypothetical_pnl - actual_pnl
    print(f"\n[THE DIFFERENCE]")
    if difference > 0:
        print(f"  • Using a 48% SL would have SAVED you ${abs(difference):.2f}")
    else:
        print(f"  • Using a 48% SL would have COST you ${abs(difference):.2f}")

if __name__ == "__main__":
    simulate_48_sl()
