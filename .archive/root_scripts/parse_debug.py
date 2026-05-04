import asyncio
import aiohttp
import json

async def main():
    async with aiohttp.ClientSession() as session:
        url = "https://gamma-api.polymarket.com/events?closed=false&limit=20&tags_slug=crypto"
        async with session.get(url) as resp:
            data = await resp.json()
            for event in data:
                for market in event.get("markets", []):
                    title = market.get("question", "")
                    if "Up or Down" in title or "PM" in title:
                        outcomes = market.get("outcomes", "[]")
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        prices = market.get("outcomePrices", "[]")
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        print(f"Q: {title[:40]}")
                        print(f"  Outcomes: {outcomes}")
                        print(f"  Prices: {prices}")
                        print(f"  Tokens: {market.get('clobTokenIds', '[]')}")

asyncio.run(main())
