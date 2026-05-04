import asyncio, aiohttp, time

async def check():
    url = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100'
    all_markets = []
    async with aiohttp.ClientSession() as session:
        offset = 0
        while offset < 500: # Scan top 500 directly from markets
            async with session.get(f"{url}&offset={offset}") as resp:
                data = await resp.json()
                if not data: break
                all_markets.extend(data)
                offset += 100
        
    print(f"Scanned {len(all_markets)} markets.")
    now = time.time()
    for m in all_markets:
        q = m.get('question', '')
        if any(x in q.lower() for x in ["bitcoin", "btc", "ethereum", "eth"]):
            end = m.get('endDate')
            if not end: continue
            from datetime import datetime
            try:
                ets = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp()
                diff = (ets - now) / 60
                # Filter for ones closing soon
                if diff < 120 and diff > 0:
                     print(f"FOUND: {q} | Closes in: {diff:.1f}m")
            except: pass

if __name__ == '__main__':
    asyncio.run(check())
