import sqlite3
db = sqlite3.connect('/home/ubuntu/reversebot/data/reversebot_rd.db')
cursor = db.cursor()
cursor.execute('SELECT DISTINCT order_status FROM paper_positions')
print("Paper order statuses:", cursor.fetchall())
