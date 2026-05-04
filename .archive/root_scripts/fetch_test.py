import asyncio
import aiohttp
import json

async def main():
    url = "https://gamma-api.polymarket.com/markets?closed=false&active=true&limit=1000"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if isinstance(data, list):
                for market in data:
                    title = market.get("question", "")
                    if "Up or Down" in title:
                        outcomes = market.get("outcomes", "[]")
                        prices = market.get("outcomePrices", "[]")
                        tokens = market.get("clobTokenIds", "[]")
                        print(f"\nMarket Slug: {market.get('slug')}")
                        print(f"  M: {title[:60]}")
                        print(f"    Outcomes: {outcomes}")
                        print(f"    Prices: {prices}")
                        print(f"    Tokens: {tokens}")
                        break
            else:
                print("Data is not a list. Keys:", data.keys())

asyncio.run(main())
