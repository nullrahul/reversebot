import sqlite3
db_path = '/home/ubuntu/reversebot/data/reversebot_rd.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
query = """
SELECT m.question, p.realised_pnl, p.close_reason, p.opened_at, p.closed_at, p.entry_price, p.exit_price
FROM paper_positions p 
JOIN markets m ON p.market_id = m.market_id 
WHERE p.order_status = 'FILLED' AND p.closed_at IS NOT NULL
ORDER BY p.closed_at DESC LIMIT 20
"""
rows = conn.execute(query).fetchall()
print(f"{'Market Question':<40} | {'PnL':<8} | {'Entry':<6} | {'Exit':<6} | {'Reason':<12} | {'Dur'}")
print("-" * 100)
for row in rows:
    duration = row['closed_at'] - row['opened_at']
    m, s = divmod(duration, 60)
    duration_str = f"{m}m {s}s" if m > 0 else f"{s}s"
    print(f"{row['question'][:40]:<40} | {row['realised_pnl']:<8.4f} | {row['entry_price']:<6.4f} | {row['exit_price']:<6.4f} | {row['close_reason']:<12} | {duration_str}")
conn.close()
