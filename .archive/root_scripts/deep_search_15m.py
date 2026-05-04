import asyncio, aiohttp, time

async def check():
    # Attempt to find ANY 15m crypto event by brute forcing possible slugs based on current hour
    import math
    now_ts = time.time()
    base_ts = math.floor(now_ts / 900) * 900
    
    coins = ['btc', 'eth', 'sol', 'doge', 'bnb', 'xrp', 'hype']
    potential_slugs = []
    # Check 1 hour worth of 15m blocks (4 blocks)
    for i in range(-4, 12):
        ts = base_ts + (i * 900)
        for c in coins:
            potential_slugs.append(f"{c}-updown-15m-{ts}")

    print(f"Brute forcing {len(potential_slugs)} potential 15m slugs...")
    async with aiohttp.ClientSession(base_url='https://gamma-api.polymarket.com') as session:
        for slug in potential_slugs:
            async with session.get(f'/events', params={'slug': slug}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        print(f"[FOUND] Valid 15m Event: {slug}")
                        for m in data[0].get('markets', []):
                             print(f"  - Market: {m.get('question')}")

if __name__ == '__main__':
    asyncio.run(check())
