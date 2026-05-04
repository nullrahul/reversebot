import asyncio
import aiohttp
import json
import logging

logging.basicConfig(level=logging.INFO)

async def test_schemas():
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    token_id = "93592949212798121127213117304912625505836768562433217537850469496310204567695"
    condition_id = "0x19ee98e348c0ccb341d1b9566fa14521566e9b2ea7aed34dc407a0ec56be36a2"

    payloads = [
        # Schema 1: The "asset_ids" method
        {"assets_ids": [token_id], "type": "market"},
        
        # Schema 2: The "asset_id" method
        {"asset_id": token_id, "type": "market"},

        # Schema 3: Condition ID with "market" channel
        {"type": "subscribe", "channel": "market", "market": condition_id},
        
        # Schema 4: Condition ID with "price_change" channel
        {"type": "subscribe", "channel": "price_change", "market": condition_id},
        
        # Schema 5: Token ID with "price_change" channel
        {"type": "subscribe", "channel": "price_change", "asset_id": token_id},
        
        # Schema 6: Token ID with "price_change" channel
        {"type": "subscribe", "channel": "price_change", "token_id": token_id},
    ]

    for p in payloads:
        logging.info(f"\n--- Testing Payload: {json.dumps(p)} ---")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, timeout=5) as ws:
                    await ws.send_json(p)
                    msg = await ws.receive(timeout=3)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        logging.info(f"Response: {msg.data}")
                    else:
                        logging.info(f"Response Type: {msg.type}")
        except Exception as e:
            logging.error(f"Failed: {e}")
            
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(test_schemas())
