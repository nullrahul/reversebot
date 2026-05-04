import sqlite3
import aiohttp
import asyncio
import json

async def check_outcomes(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT p.position_id, p.market_id, p.notes as token_id, m.question FROM paper_positions p JOIN markets m ON p.market_id = m.market_id WHERE p.order_status='FILLED'").fetchall()
    
    async with aiohttp.ClientSession() as session:
        for r in rows:
            mid = r['market_id']
            url = f"https://gamma-api.polymarket.com/markets/{mid}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = data.get("outcomePrices", "[]")
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    
                    # outcomePrices[0] = YES, outcomePrices[1] = NO
                    p_yes = float(prices[0])
                    p_no = float(prices[1])
                    
                    # Check which token the bot bought
                    tokens = data.get("tokens", [])
                    bot_outcome = "Unknown"
                    for t in tokens:
                        if str(t.get("token_id")) == str(r['token_id']):
                            bot_outcome = t.get("outcome")
                    
                    print(f"Pos: {r['position_id'][:8]} | Market: {mid} | Bot Bought: {bot_outcome} | Outcome YES: {p_yes} | Outcome NO: {p_no}")
                else:
                    print(f"Failed to fetch market {mid}")

if __name__ == "__main__":
    asyncio.run(check_outcomes("/home/ubuntu/reversebot/reversebot_rd.db"))
