import sqlite3
db = sqlite3.connect('/home/ubuntu/reversebot/data/reversebot_rd.db')
cursor = db.cursor()
cursor.execute('SELECT COUNT(*) FROM positions')
print('positions:', cursor.fetchone()[0])
cursor.execute('SELECT COUNT(*) FROM paper_positions')
print('paper_positions:', cursor.fetchone()[0])
