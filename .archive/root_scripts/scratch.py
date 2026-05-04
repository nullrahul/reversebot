import asyncio
import aiohttp
import json

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

async def fetch_markets():
    async with aiohttp.ClientSession() as session:
        params = {"limit": 100, "active": "true", "closed": "false", "search": "Up or Down", "order": "volume24hr", "ascending": "false"}
        async with session.get(GAMMA_API_URL, params=params) as resp:
            data = await resp.json()
            for m in data[:5]:
                print(f"Q: {m.get('question')}")
                print(f"Start: {m.get('startDate')} | End: {m.get('endDate')} | Closes: {m.get('closes_at')}")

asyncio.run(fetch_markets())
