import asyncio
import aiohttp
import json
import datetime
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
GAMMA_API = "https://gamma-api.polymarket.com"

async def fetch_slug(session, slug):
    url = f"{GAMMA_API}/events?slug={slug}"
    try:
        async with session.get(url, timeout=5) as r:
            if r.status == 200:
                data = await r.json()
                if data and len(data) > 0:
                    ev = data[0]
                    markets = ev.get("markets", [])
                    if markets:
                        m = markets[0]
                        res = "?"
                        for t in m.get("tokens", []):
                            if t.get("winner") == True:
                                outcome = str(t.get("outcome", "")).upper()
                                if outcome in ["YES", "UP"]: res = "UP"
                                if outcome in ["NO", "DOWN"]: res = "DOWN"
                        if res == "?":
                            op = m.get("outcomePrices")
                            if op:
                                op_list = json.loads(op) if isinstance(op, str) else op
                                if op_list and len(op_list) > 0:
                                    yes_final = float(op_list[0])
                                    if yes_final > 0.5: res = "UP"
                                    elif yes_final < 0.5: res = "DOWN"
                                    else: res = "TIE"
                                    
                        return {
                            "slug": slug,
                            "timestamp": int(slug.split('-')[-1]),
                            "resolution": res,
                            "title": ev.get("title")
                        }
    except Exception:
        pass
    return None

async def main():
    configs = [("btc", 5), ("eth", 5), ("btc", 15), ("eth", 15)]
    
    # May 23 00:00:00 UTC to May 24 23:59:59 UTC
    start_dt = datetime.datetime(2026, 5, 23, 0, 0, 0, tzinfo=datetime.timezone.utc)
    end_dt = datetime.datetime(2026, 5, 24, 23, 59, 59, tzinfo=datetime.timezone.utc)
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    if end_dt > now:
        end_dt = now
        
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    
    all_data = {}
    
    async with aiohttp.ClientSession() as session:
        for coin, duration in configs:
            slugs = []
            dur_secs = duration * 60
            curr_ts = start_ts
            while curr_ts <= end_ts:
                slugs.append(f"{coin}-updown-{duration}m-{curr_ts}")
                curr_ts += dur_secs
                
            logging.info(f"Fetching {len(slugs)} markets for {coin.upper()} {duration}m...")
            
            markets = []
            chunk_size = 50
            for i in range(0, len(slugs), chunk_size):
                chunk = slugs[i:i+chunk_size]
                tasks = [fetch_slug(session, s) for s in chunk]
                res = await asyncio.gather(*tasks)
                markets.extend([m for m in res if m])
                await asyncio.sleep(0.1)
                
            markets.sort(key=lambda x: x["timestamp"])
            all_data[f"{coin}_{duration}m"] = markets

    # Create TXT Export
    with open("data/market_sequences.txt", "w") as f:
        f.write("POLYMARKET RESOLUTION SEQUENCES (MAY 23 & MAY 24)\n")
        f.write("=================================================\n\n")
        
        for key, markets in all_data.items():
            f.write(f"--- {key.upper()} MARKETS ---\n")
            
            # Create a horizontal sequence for visual AI parsing
            horizontal = []
            for m in markets:
                if m["resolution"] == "UP": horizontal.append("UP ")
                elif m["resolution"] == "DOWN": horizontal.append("DN ")
                elif m["resolution"] == "TIE": horizontal.append("== ")
                else: horizontal.append("?? ")
            
            f.write("Visual Sequence:\n")
            f.write("".join(horizontal) + "\n\n")
            
            f.write("Detailed Sequence:\n")
            for m in markets:
                if m["resolution"] in ["UP", "DOWN", "TIE"]:
                    dt_str = datetime.datetime.fromtimestamp(m["timestamp"], tz=datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                    color = "🟩 UP  " if m["resolution"] == "UP" else ("🟥 DOWN" if m["resolution"] == "DOWN" else "⬜ TIE")
                    f.write(f"{dt_str} | {color}\n")
            f.write("\n")

    # Save to JSON
    with open("data/market_sequences.json", "w") as f:
        json.dump(all_data, f, indent=2)
        
    logging.info("\nData exported to data/market_sequences.txt and data/market_sequences.json")

if __name__ == "__main__":
    asyncio.run(main())
