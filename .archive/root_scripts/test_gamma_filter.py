import asyncio
import aiohttp
import time
from datetime import datetime

async def test_filter():
    async with aiohttp.ClientSession() as session:
        params = {"limit": 100, "active": "true", "closed": "false", "search": "Up or Down", "order": "createdAt", "ascending": "false"}
        print("Fetching 'Up or Down' from Gamma...")
        async with session.get("https://gamma-api.polymarket.com/markets", params=params) as resp:
            data = await resp.json()
            
            raw_count = len(data) if isinstance(data, list) else 0
            print(f"Raw 'Up or Down' markets fetched: {raw_count}")
            
            if raw_count == 0: return
            
            filtered = []
            for raw in data:
                q = raw.get("question", "").lower()
                if "up or down" in q:
                    # SIMULATE THE EXACT FILTER
                    if "bitcoin" in q or "btc" in q or "ethereum" in q or "eth" in q:
                        continue # Skip btc/eth
                    
                    closes_at_str = raw.get("endDate")
                    if not closes_at_str: continue
                    
                    try:
                        closes_at = datetime.fromisoformat(closes_at_str.replace('Z', '+00:00')).timestamp()
                    except:
                        continue
                    
                    now = time.time()
                    if closes_at > now and (closes_at - now) <= 900: # 15 minutes max
                        filtered.append(raw)
                        
            print(f"Filtered Short-Window Non-BTC/ETH markets found: {len(filtered)}")
            for m in filtered:
                print(f" -> {m.get('question')} (Closes in {int((datetime.fromisoformat(m.get('endDate').replace('Z', '')) - datetime.utcnow()).total_seconds())}s)")
                
asyncio.run(test_filter())
