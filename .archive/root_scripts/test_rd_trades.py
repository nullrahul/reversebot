import sqlite3
conn = sqlite3.connect('data/reversebot_rd.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT p.position_id, m.question, m.closes_at, p.opened_at, p.closed_at, p.realised_pnl, p.close_reason, p.order_status FROM paper_positions p JOIN markets m ON p.market_id = m.market_id ORDER BY p.opened_at DESC LIMIT 20').fetchall()
print(f'Total fetched: {len(rows)}')
for r in rows:
    print(f'Q: {r["question"]} | PNL: {r["realised_pnl"]} | STATUS: {r["order_status"]} | REASON: {r["close_reason"]} | OP: {r["opened_at"]} | CL: {r["closes_at"]}')
conn.close()
