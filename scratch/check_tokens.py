import sqlite3
import json

def check_tokens(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    print(f"--- Token ID Check: {db_path} ---")
    rows = conn.execute("SELECT position_id, notes, market_id FROM paper_positions ORDER BY opened_at DESC LIMIT 10").fetchall()
    for r in rows:
        print(f"Pos: {r['position_id'][:8]} | Market: {r['market_id']} | Notes: {r['notes']}")

if __name__ == "__main__":
    check_tokens("/home/ubuntu/reversebot/reversebot_rd.db")
