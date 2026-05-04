import asyncio
import aiohttp
import json

async def test_ws():
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect("wss://ws-subscriptions-clob.polymarket.com/ws/market")
        print("Connected!")
        # Subscribe to a known active token, we need a token_id.
        # Let's subscribe to a random market channel
        # We can subscribe to market: 93592949212798121127213117304912625505836768562433217537850469496310204567695 (From user's dump)
        token_id = "93592949212798121127213117304912625505836768562433217537850469496310204567695"
        market_id = "0x19ee98e348c0ccb341d1b9566fa14521566e9b2ea7aed34dc407a0ec56be36a2"
        await ws.send_json({"assets_ids": [token_id], "type": "market"}) # Valid format?
        print("Sent assets_ids subscribe")
        
        # Try Polymarket documented format
        msg = {
            "type": "subscribe",
            "channel": "price_change", # or market
            "market": market_id
        }
        await ws.send_json(msg)
        print("Sent channel subscribe")
        
        for _ in range(5):
            msg = await ws.receive()
            print("Received:", msg.data)

        await ws.close()

if __name__ == "__main__":
    asyncio.run(test_ws())
