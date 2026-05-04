import sqlite3
import json

db_path = 'reversebot_rd.db'

def analyze():
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    
    report = {}
    
    # Check tables first
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    report['tables'] = tables

    # Latest 30 trades (checking both tables just in case)
    # Using 'paper_positions' as primary for R&D
    sql = """
        SELECT p.position_id, p.entry_price, p.exit_price, p.shares_bought, p.size_usdc, p.realised_pnl, p.close_reason, p.opened_at, m.question
        FROM paper_positions p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status = 'FILLED'
        ORDER BY p.closed_at DESC
        LIMIT 30
    """
    try:
        report['trades'] = [dict(r) for r in db.execute(sql).fetchall()]
    except Exception as e:
        report['error'] = str(e)
    
    print(json.dumps(report, indent=2))
    db.close()

if __name__ == "__main__":
    analyze()
