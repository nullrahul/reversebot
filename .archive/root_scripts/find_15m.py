import asyncio, aiohttp

async def check():
    # Fetch all active markets and look for 15m ones
    url = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            markets = await resp.json()
            found = 0
            for m in markets:
                q = m.get('question', '')
                if "15-minute" in q or "15m" in q.lower():
                    found += 1
                    slug = m.get('slug', 'N/A')
                    print(f"15m MARKET: {q}")
                    print(f"  - SLUG: {slug}")
            if found == 0:
                print("No 15m markets found in top 100 via search. Checking event slugs...")

if __name__ == '__main__':
    asyncio.run(check())
