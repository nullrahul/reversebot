import sqlite3
import json
import asyncio
import aiohttp
import sys
import os
import time

# Robust DB Path
DB_PATH = "data/reversebot_rd.db"
if not os.path.exists(DB_PATH):
    DB_PATH = "../data/reversebot_rd.db"

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

class LiveMonitor:
    def __init__(self):
        self.markets = {} # market_id -> {question, entry, y_tid, n_tid}
        self.token_prices = {} # token_id -> {bid, ask, last, updated_at}
        self.last_db_refresh = 0

    async def get_gamma_price(self, mid):
        """Fetch current prices from Gamma API to seed the monitor."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{GAMMA_URL}/{mid}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {
                            "yes": float(data.get("yes_price", 0)),
                            "no": float(data.get("no_price", 0))
                        }
        except: pass
        return None

    def refresh_markets_from_db(self):
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT m.market_id, m.question, m.yes_token_id, m.no_token_id, p.entry_price
                FROM paper_positions p
                JOIN markets m ON p.market_id = m.market_id
                WHERE p.closed_at IS NULL AND p.order_status IN ('OPEN', 'FILLED', 'PARTIAL_FILLED')
            ''')
            rows = cursor.fetchall()
            conn.close()

            new_markets = {}
            tids = set()
            for r in rows:
                mid = r["market_id"]
                y_tid = r["yes_token_id"]
                n_tid = r["no_token_id"]
                new_markets[mid] = {"q": r["question"], "entry": r["entry_price"], "y_tid": y_tid, "n_tid": n_tid}
                if y_tid: tids.add(y_tid)
                if n_tid: tids.add(n_tid)
                if y_tid not in self.token_prices: self.token_prices[y_tid] = {"bid": 0, "ask": 0, "last": 0, "upd": 0}
                if n_tid not in self.token_prices: self.token_prices[n_tid] = {"bid": 0, "ask": 0, "last": 0, "upd": 0}

            changed = set(new_markets.keys()) != set(self.markets.keys())
            self.markets = new_markets
            return list(tids), changed
        except Exception as e:
            return [], False

    async def run(self):
        active_tids, _ = self.refresh_markets_from_db()
        print(f"[INIT] Scanning {len(self.markets)} open positions...", flush=True)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                async def subscribe():
                    tids, _ = self.refresh_markets_from_db()
                    if not tids: return
                    await ws.send_json({"operation": "subscribe", "assets_ids": tids, "custom_feature_enabled": True})
                    await ws.send_json({"operation": "subscribe", "assets_ids": tids, "type": "best_bid_ask"})
                    # Seed prices from Gamma
                    for mid in self.markets:
                        p = await self.get_gamma_price(mid)
                        if p:
                            m = self.markets[mid]
                            if m["y_tid"]: self.token_prices[m["y_tid"]]["last"] = p["yes"]
                            if m["n_tid"]: self.token_prices[m["n_tid"]]["last"] = p["no"]

                await subscribe()
                asyncio.create_task(self.heartbeat(ws))
                
                # Ticker Loop
                async def ticker():
                    while not ws.closed:
                        await asyncio.sleep(1)
                        if not self.markets: continue
                        print("\033[H\033[J", end="") # Refresh Screen
                        print("="*100)
                        print(f"{'MARKET (OPEN POSITIONS)':<45} | {'ENTRY':<7} | {'YES-BID':<7} | {'NO-BID':<7} | {'TRIGGER':<10} | {'PNL':<7}")
                        print("-"*100)
                        for mid, m in self.markets.items():
                            y, n = self.token_prices.get(m["y_tid"], {}), self.token_prices.get(m["n_tid"], {})
                            
                            # BOT LOGIC: BID -> IMPLIED -> LAST
                            trigger_px, src = n.get("bid", 0), "BID"
                            if (not trigger_px or trigger_px <= 0) and y.get("ask"):
                                trigger_px, src = round(1.0 - y["ask"], 4), "IMP"
                            if (not trigger_px or trigger_px <= 0):
                                trigger_px, src = n.get("last", 0), "LST"
                            
                            pnl = (trigger_px - m["entry"]) / m["entry"] * 100 if m["entry"] > 0 and trigger_px > 0 else 0
                            c = "\033[92m" if pnl >= 0 else "\033[91m"
                            pnl_str = f"{c}{pnl:>+6.1f}% \033[0m" if trigger_px > 0 else "WAITING"
                            
                            print(f"{m['q'][:45]:<45} | ${m['entry']:.3f} | ${y.get('bid',0):.3f} | ${n.get('bid',0):.3f} | ${trigger_px:.3f} {src:<3} | {pnl_str}")
                        print("\n[INFO] DB Refresh every 2s | WS Connected | Press Ctrl+C to Exit", flush=True)

                asyncio.create_task(ticker())

                # DB/Sub Watcher
                async def watch():
                    while not ws.closed:
                        await asyncio.sleep(2)
                        tids, changed = self.refresh_markets_from_db()
                        if changed: await subscribe()
                asyncio.create_task(watch())

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            events = data if isinstance(data, list) else [data]
                            for d in events:
                                tid = str(d.get("asset_id") or d.get("token_id") or d.get("market_id") or "")
                                if tid in self.token_prices:
                                    etype = d.get("event_type") or d.get("type")
                                    if etype == "best_bid_ask":
                                        if d.get("best_bid"): self.token_prices[tid]["bid"] = float(d["best_bid"])
                                        if d.get("best_ask"): self.token_prices[tid]["ask"] = float(d["best_ask"])
                                    elif etype == "price_change":
                                        if d.get("side") == "BUY": self.token_prices[tid]["bid"] = float(d["price"])
                                        else: self.token_prices[tid]["ask"] = float(d["price"])
                                    elif d.get("price"):
                                        self.token_prices[tid]["last"] = float(d["price"])
                        except: pass

    async def heartbeat(self, ws):
        while not ws.closed:
            await ws.send_str("PING")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try: asyncio.run(LiveMonitor().run())
    except KeyboardInterrupt: print("\nExiting...")