import sqlite3

db = sqlite3.connect('/home/ubuntu/reversebot/data/reversebot_rd.db')
cursor = db.cursor()
cursor.execute("DELETE FROM paper_positions WHERE order_status = 'FILLED' AND realised_pnl > 1.44 AND realised_pnl < 1.46")
deleted = cursor.rowcount
db.commit()

cursor.execute("SELECT COUNT(*) FROM paper_positions")
total = cursor.fetchone()[0]
print(f"Deleted {deleted} fake wins. Remaining paper_positions: {total}")
db.close()
