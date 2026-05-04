import asyncio, aiohttp, sqlite3

async def check():
    conn = sqlite3.connect('data/reversebot_rd.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT market_id, question FROM markets WHERE is_active = 1 LIMIT 10")
    active_test = cursor.fetchall()
    conn.close()

    print(f"Verifying {len(active_test)} markets from local DB against Gamma API:")
    async with aiohttp.ClientSession() as session:
        for row in active_test:
            mid = row['market_id']
            q = row['question']
            url = f'https://gamma-api.polymarket.com/markets/{mid}'
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = data.get('outcomePrices', [])
                    yp = data.get('yes_price', 'None')
                    print(f"MARKET ID: {mid} | {q[:50]}...")
                    print(f"  - Actual Gamma Prices: {prices}")
                    print(f"  - Actual Gamma Yes Price: {yp}")
                else:
                    print(f"MARKET ID: {mid} | [ERROR] API Status {resp.status}")

if __name__ == '__main__':
    asyncio.run(check())
