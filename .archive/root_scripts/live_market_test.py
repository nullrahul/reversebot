import asyncio
import aiohttp
import re
import time
from datetime import datetime

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

def is_match(q: str) -> bool:
    q = q.lower()
    if not any(coin in q for coin in ["bitcoin", "btc", "ethereum", "eth"]): return False
    if re.search(r"\b(5|15)\s*(?:m|min|minute|minutes)\b", q): return True
    if re.search(r"(?::(00|05|15|30|45))\s*(?:am|pm)", q) and ("up or down" in q or "above" in q or "below" in q): return True
    if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)\s*-\s*\d{1,2}:\d{2}\s*(?:am|pm)", q): return True
    return False

async def fetch_live():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] INITIATING LIVE GAMMA SWEEP FOR SHORT WINDOW MARKETS\n" + "="*70)
    async with aiohttp.ClientSession() as session:
        found_markets = []
        queries = ["Bitcoin", "Ethereum", "Up or Down", "5m", "15m"]
        for q_str in queries:
            params = {"limit": 100, "active": "true", "closed": "false", "search": q_str, "order": "createdAt", "ascending": "false"}
            async with session.get(GAMMA_API_URL, params=params) as resp:
                data = await resp.json()
                if not isinstance(data, list): continue
                for m in data:
                    if is_match(m.get("question", "")):
                        market_id = m.get("id", m.get("condition_id"))
                        if not any(x["id"] == market_id for x in found_markets):
                            found_markets.append({
                                "id": market_id,
                                "q": m.get("question", ""),
                                "closes_at": m.get("endDate"),
                                "vol": m.get("volume", "0")
                            })
        
        print(f"✅ SUCCESSFULLY VALIDATED {len(found_markets)} ACTIVE SHORT-WINDOW CRYPTO MARKETS!")
        print("-" * 70)
        
        for idx, m in enumerate(found_markets[:15], 1):
            closes = m['closes_at'] or 'Unknown'
            print(f"{idx}. MARKET: {m['q']}")
            print(f"   CLOSES: {closes} | CLOB ID: {m['id'][:10]}... | VOL: ${float(m['vol']):.2f}")
            print("-" * 70)

        if len(found_markets) > 15:
            print(f"... and {len(found_markets)-15} more unlisted here.")

asyncio.run(fetch_live())
