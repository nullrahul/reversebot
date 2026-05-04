import sqlite3
db = sqlite3.connect('/home/ubuntu/reversebot/data/reversebot_rd.db')
cursor = db.cursor()
query = """
SELECT m.question, p.size_usdc, p.shares_bought, p.realised_pnl, p.close_reason, p.closed_at
FROM paper_positions p 
JOIN markets m ON p.market_id = m.market_id 
"""
cursor.execute(query)
rows = cursor.fetchall()
if not rows:
    print("No closed paper positions found.")
for row in rows:
    pnl = f"${row[3]:.2f}" if row[3] is not None else "None"
    print(f"✅ {row[0]}")
    print(f"   ${row[1]} | {row[2]} shares | Realized PnL = {pnl} | Reason: {row[4]} | closed_at: {row[5]}")
