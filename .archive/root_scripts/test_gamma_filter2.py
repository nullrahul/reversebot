import asyncio
import aiohttp
import time
from datetime import datetime
import re

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

def is_crypto_short_window(question: str) -> bool:
    q_lower = question.lower()
    if not any(coin in q_lower for coin in ["bitcoin", "btc", "ethereum", "eth"]): return False
    if re.search(r"\b(5|15)\s*(?:m|min|minute|minutes)\b", q_lower): return True
    if re.search(r"(?::(00|05|15|30|45))\s*(?:am|pm)", q_lower):
        if "up or down" in q_lower or "above" in q_lower or "below" in q_lower: return True
    if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)\s*-\s*\d{1,2}:\d{2}\s*(?:am|pm)", q_lower): return True
    return False

async def fetch_and_filter_markets():
    print("Fetching active markets from Gamma API...")
    async with aiohttp.ClientSession() as session:
        queries = ["Bitcoin", "Ethereum", "Up or Down", "5m", "15m"]
        all_short_markets = set()
        
        for q in queries:
            params = {"limit": 100, "active": "true", "closed": "false", "search": q, "order": "createdAt", "ascending": "false"}
            async with session.get(GAMMA_API_URL, params=params) as resp:
                markets = await resp.json()
                if not isinstance(markets, list): continue
                for m in markets:
                    if is_crypto_short_window(m.get("question", "")):
                        all_short_markets.add(m.get("question", ""))
                        
        print(f"FOUND: {len(all_short_markets)}")
        for i in all_short_markets: print(f" - {i}")

asyncio.run(fetch_and_filter_markets())
