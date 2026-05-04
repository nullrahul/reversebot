import sqlite3
db = sqlite3.connect('data/reversebot.db')
c = db.cursor()
c.execute("SELECT COUNT(*) FROM positions WHERE order_status IN ('OPEN', 'PENDING')")
print('Live:', c.fetchone()[0])
try:
    c.execute("SELECT COUNT(*) FROM paper_positions WHERE order_status IN ('OPEN', 'PENDING')")
    print('Paper:', c.fetchone()[0])
except: pass
