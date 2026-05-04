import asyncio, aiohttp

async def check():
    url = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            markets = await resp.json()
            for m in markets:
                q = m.get('question', '')
                if "Up or Down" in q:
                    # Prices are in the outcomePrices field (often) or we need CLOB
                    op = m.get('outcomePrices', [])
                    print(f"MARKET: {q[:50]}... | Gamma Prices: {op}")

if __name__ == '__main__':
    asyncio.run(check())
