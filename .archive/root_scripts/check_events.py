import asyncio, aiohttp, time
from datetime import datetime

async def check():
    now = time.time()
    # Fetch events instead of markets to see the 'grouping'
    url = 'https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for event in data:
                title = event.get('title', '')
                if any(x in title.lower() for x in ["bitcoin", "ethereum", "crypto"]):
                    for m in event.get('markets', []):
                        q = m.get('question', '')
                        if "up or down" in q.lower() or "5m" in q.lower() or "15m" in q.lower():
                            end = m.get('endDate')
                            if not end: continue
                            ets = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp()
                            diff = (ets - now) / 60
                            print(f"EVENT: {title} | MARKET: {q[:40]}... | Closes in: {diff:.1f} mins")

if __name__ == '__main__':
    asyncio.run(check())
