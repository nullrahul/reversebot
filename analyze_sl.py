import sqlite3
import datetime
import statistics

DB_PATH = 'data/reversebot_rd.db'

def analyze_excursions():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get start of May 22, 2026 UTC
    dt_start = datetime.datetime(2026, 5, 22, 0, 0, 0, tzinfo=datetime.timezone.utc)
    ts_start = int(dt_start.timestamp())
    
    # Also fetch the question to filter out any null/bad data
    query = """
        SELECT 
            p.position_id, p.side, p.entry_price, p.realised_pnl, 
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
        print("No trades found with max/min price logging for yesterday.")
        return

    winners = []
    losers = []
    
    for r in rows:
        t = dict(r)
        entry = t['entry_price']
        min_p = t['min_price_seen']
        max_p = t['max_price_seen']
        
        # Avoid division by zero
        if entry <= 0: continue
            
        # Maximum Adverse Excursion (Drop from entry)
        dip_pct = ((entry - min_p) / entry) * 100
        # Maximum Favorable Excursion (Rise from entry)
        pump_pct = ((max_p - entry) / entry) * 100
        
        t['dip_pct'] = dip_pct
        t['pump_pct'] = pump_pct
        
        if t['realised_pnl'] > 0:
            winners.append(t)
        else:
            losers.append(t)

    print("=" * 60)
    print(f"STOP LOSS OPTIMIZATION ANALYSIS (Based on {len(rows)} trades)")
    print("=" * 60)
    
    print(f"\n[WINNING TRADES] - Count: {len(winners)}")
    if winners:
        dips = [w['dip_pct'] for w in winners]
        max_dip_endured = max(dips)
        avg_dip = statistics.mean(dips)
        median_dip = statistics.median(dips)
        
        print(f"  • To win, these trades had to endure temporary dips.")
        print(f"  • Average dip before winning: -{avg_dip:.1f}%")
        print(f"  • Median dip before winning:  -{median_dip:.1f}%")
        print(f"  • Maximum dip endured:        -{max_dip_endured:.1f}% (The closest call)")
        
        # Calculate safe SL levels
        print("\n  If you set a Stop Loss at X% drop, here is how many winners you would have accidentally killed:")
        for sl_level in [5, 10, 15, 20, 25, 30]:
            killed = sum(1 for d in dips if d > sl_level)
            print(f"    - SL at {sl_level}%: Kills {killed} out of {len(winners)} winning trades ({(killed/len(winners))*100:.1f}%)")

    print(f"\n[LOSING TRADES] - Count: {len(losers)}")
    if losers:
        pumps = [l['pump_pct'] for l in losers]
        dips = [l['dip_pct'] for l in losers]
        
        avg_pump = statistics.mean(pumps)
        max_pump = max(pumps)
        
        print(f"  • Did these losers ever show a profit before dying?")
        print(f"  • Average max profit seen: +{avg_pump:.1f}%")
        print(f"  • Maximum profit seen:     +{max_pump:.1f}%")
        
        print("\n  If you set a Stop Loss at X% drop, here is how many losers you would have cut early (saving money):")
        for sl_level in [5, 10, 15, 20, 25, 30]:
            saved = sum(1 for d in dips if d >= sl_level) # It dropped past SL
            print(f"    - SL at {sl_level}%: Cuts {saved} out of {len(losers)} losers early ({(saved/len(losers))*100:.1f}%)")

    print("\n" + "=" * 60)
    print("RECOMMENDATION ENGINE")
    print("=" * 60)
    if winners and losers:
        # We want to find an SL that cuts the most losers while killing the fewest winners.
        best_sl = 0
        best_score = -999
        for sl in range(1, 50):
            killed_winners = sum(1 for d in [w['dip_pct'] for w in winners] if d > sl)
            saved_losers = sum(1 for d in [l['dip_pct'] for l in losers] if d >= sl)
            
            # Simple heuristic: Every saved loser is +1 point, every killed winner is -1 point
            # Adjust weighting based on average win/loss sizes if needed.
            score = saved_losers - (killed_winners * 1.5) # Penalize killing winners more
            
            if score > best_score:
                best_score = score
                best_sl = sl
                
        print(f"Based on historical excursion data, the optimal mathematical Stop Loss is: -{best_sl}%")
        kw = sum(1 for d in [w['dip_pct'] for w in winners] if d > best_sl)
        sl_saved = sum(1 for d in [l['dip_pct'] for l in losers] if d >= best_sl)
        print(f"  • This setting cuts {sl_saved} losers early.")
        print(f"  • It sacrifices {kw} winners.")

if __name__ == "__main__":
    analyze_excursions()
