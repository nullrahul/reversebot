import asyncio, aiohttp, time

async def check():
    # Fetch all active Up/Down markets
    url = 'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            markets = await resp.json()
            up_down = [m for m in markets if "Up or Down" in m.get('question', '')]
            
            print(f"Checking {len(up_down)} active Up/Down markets:")
            for m in up_down:
                q = m.get('question', '')
                p = m.get('outcomePrices', []) # This returns [yes_price, no_price]
                yp = m.get('yes_price')
                # Polymarket also has an outcomePrices field that is often [0.5, 0.5] if no trades
                print(f"MARKET: {q[:60]}...")
                print(f"  - outcomePrices: {p}")
                print(f"  - yes_price attribute: {yp}")
                if p == ["0.5", "0.5"] or p == [0.5, 0.5]:
                     print("  - [STATUS] Initial/Stale state (No trades yet)")
                elif yp is None:
                     print("  - [STATUS] Missing price data in Gamma API")

if __name__ == '__main__':
    asyncio.run(check())
