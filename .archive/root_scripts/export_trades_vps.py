import sqlite3
import json
import os

db_path = '/home/ubuntu/reversebot/data/reversebot.db'

def export():
    if not os.path.exists(db_path):
        print(json.dumps({"error": f"DB not found at {db_path}"}))
        return

    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        
        # Pull last 30 FILLED paper positions with their market context
        sql = """
            SELECT p.position_id, p.market_id, p.entry_price, p.exit_price, 
                   p.shares_bought, p.size_usdc, p.realised_pnl, p.close_reason, 
                   p.opened_at, p.closed_at, m.question
            FROM paper_positions p
            LEFT JOIN markets m ON p.market_id = m.market_id
            WHERE p.order_status = 'FILLED'
            ORDER BY p.closed_at DESC
            LIMIT 30
        """
        rows = db.execute(sql).fetchall()
        data = [dict(r) for r in rows]
        print(json.dumps(data))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        db.close()

if __name__ == "__main__":
    export()
