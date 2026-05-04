import sqlite3
import json

db_path = 'reversebot_dump.db'

def analyze():
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    
    report = {}
    
    # 1. Row counts
    report['counts'] = {
        'positions': db.execute('SELECT COUNT(*) FROM positions').fetchone()[0],
        'paper_positions': db.execute('SELECT COUNT(*) FROM paper_positions').fetchone()[0]
    }
    
    # 2. Latest 20 paper trades
    sql = """
        SELECT p.position_id, p.entry_price, p.exit_price, p.shares_bought, p.size_usdc, p.realised_pnl, p.close_reason, p.opened_at, m.question
        FROM paper_positions p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status = 'FILLED'
        ORDER BY p.closed_at DESC
        LIMIT 20
    """
    report['latest_paper_trades'] = [dict(r) for r in db.execute(sql).fetchall()]
    
    # 3. Latest 20 live trades
    sql = """
        SELECT p.position_id, p.entry_price, p.exit_price, p.shares_bought, p.size_usdc, p.realised_pnl, p.close_reason, p.opened_at, m.question
        FROM positions p
        LEFT JOIN markets m ON p.market_id = m.market_id
        WHERE p.order_status = 'FILLED'
        ORDER BY p.closed_at DESC
        LIMIT 20
    """
    report['latest_live_trades'] = [dict(r) for r in db.execute(sql).fetchall()]
    
    print(json.dumps(report, indent=2))
    db.close()

if __name__ == "__main__":
    analyze()
