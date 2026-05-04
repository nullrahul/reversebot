import sqlite3

conn = sqlite3.connect('data/reversebot.db')
conn.row_factory = sqlite3.Row

query = '''
    SELECT 
        m.category, 
        COUNT(p.position_id) as total_trades,
        SUM(p.realised_pnl) as total_pnl,
        SUM(CASE WHEN p.realised_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        SUM(CASE WHEN p.realised_pnl < 0 THEN 1 ELSE 0 END) as losing_trades
    FROM paper_positions p
    JOIN markets m ON p.market_id = m.market_id
    WHERE p.order_status IN ('FILLED', 'CLOSED')
    GROUP BY m.category
    ORDER BY total_pnl DESC
'''

rows = conn.execute(query).fetchall()

print('CATEGORY | TOTAL_TRADES | WINS | LOSSES | PNL')
print('-' * 60)

for r in rows:
    cat = r['category'] or 'Unknown'
    pnl = r['total_pnl'] or 0.0
    wins = r['winning_trades'] or 0
    losses = r['losing_trades'] or 0
    print(f'{cat[:20]:<20} | {r["total_trades"]:^12} | {wins:<4} | {losses:<6} | {pnl:.4f}')

conn.close()
