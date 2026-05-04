import asyncio
import sqlite3
import re

def get_core_context(question):
    q = re.sub(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b', '', question, flags=re.I)
    q = re.sub(r'\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))?\s*(?:ET|UTC|UTC\d+)?', '', q, flags=re.I)
    q = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '', q)
    q = re.sub(r'\b202\d\b', '', q)
    q = re.sub(r'\s+on\s*$', '', q, flags=re.I)
    q = re.sub(r'\s+by\s*$', '', q, flags=re.I)
    q = re.sub(r'\?$', '', q)
    q = re.sub(r'^Will\s+', '', q, flags=re.I)
    return ' '.join(q.split()).strip().lower()

async def cleanup():
    conn = sqlite3.connect('data/reversebot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.execute('SELECT p.position_id, m.question, m.market_id, m.closes_at FROM paper_positions p JOIN markets m ON p.market_id = m.market_id WHERE p.order_status = "OPEN"')
    rows = cursor.fetchall()
    
    clusters = {}
    for r in rows:
        core = get_core_context(r['question'])
        if core not in clusters:
            clusters[core] = []
        clusters[core].append(r)
    
    for core, markets in clusters.items():
        if len(markets) > 1:
            # Sort by closes_at ascending
            markets.sort(key=lambda x: x['closes_at'] if x['closes_at'] else 9999999999)
            best = markets[0]
            to_close = markets[1:]
            
            print(f'Cluster: {core}')
            print(f'  KEEPING: {best["question"]} (Closes: {best["closes_at"]})')
            for bad in to_close:
                print(f'  CLOSING: {bad["question"]} (Closes: {bad["closes_at"]})')
                conn.execute('UPDATE paper_positions SET order_status = "CLOSED", close_reason = "CLUSTER_CLEANUP" WHERE position_id = ?', (bad['position_id'],))
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    asyncio.run(cleanup())
