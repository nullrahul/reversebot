import asyncio, aiohttp, time
from datetime import datetime

async def check():
    now = time.time()
    # Search for all "Up or Down" markets and check if ANY close today
    url = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&search=Up%20or%20Down&limit=1000'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            print(f"Current UTC: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}")
            found = 0
            for m in data:
                q = m.get('question', 'Unknown')
                end = m.get('endDate')
                if not end: continue
                ets = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp()
                diff = (ets - now) / 60
                if diff < 120 and diff > 0:
                    print(f"!!! FOUND NEARBY: {q[:60]} | Closes in: {diff:.1f} mins")
                    found += 1
                elif diff < 0:
                     pass # ignore closed
            if found == 0:
                print("Confirmed: No markets closing in the next 120 minutes were found in the top 1000 search results.")

if __name__ == '__main__':
    asyncio.run(check())
