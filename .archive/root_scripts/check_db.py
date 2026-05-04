import sqlite3
db = sqlite3.connect('/home/ubuntu/reversebot/data/reversebot_rd.db')
cursor = db.cursor()
query = """
SELECT m.question, p.size_usdc, p.shares_bought, p.realised_pnl, p.close_reason 
FROM paper_positions p 
JOIN markets m ON p.market_id = m.market_id 
WHERE p.order_status='CLOSED' 
LIMIT 15
"""
cursor.execute(query)
rows = cursor.fetchall()
if not rows:
    print("No closed paper positions found.")
for row in rows:
    print(f"✅ {row[0]}")
    print(f"   ${row[1]} | {row[2]} shares | Realized PnL = ${row[3]:.2f} | Reason: {row[4]}")
