import asyncio
import aiohttp
import json

async def main():
    async with aiohttp.ClientSession() as session:
        url = "https://gamma-api.polymarket.com/events"
        params = {"limit": 1, "closed": "false", "active": "true"}
        
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                markets = data[0].get("markets", [])
                if len(markets) > 0:
                    print(json.dumps(markets[0], indent=2))
                else:
                    print("First event had no markets!")
            else:
                print("Empty response")

if __name__ == "__main__":
    asyncio.run(main())
