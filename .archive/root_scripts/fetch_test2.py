import asyncio
from src.api.gamma_client import GammaClient
from config.config import load_config

async def main():
    config = load_config()
    gamma = GammaClient(config.gamma_api_url, config)
    markets = await gamma.fetch_ephemeral_crypto_markets(limit=10)
    for m in markets:
        print(f"Q: {m['question']}")
        print(f"  yes_price: {m.get('yes_price')}")
        print(f"  no_price: {m.get('no_price')}")
        print(f"  yes_token: {m.get('yes_token_id')}")
        print(f"  no_token: {m.get('no_token_id')}")
        print("---")
        
asyncio.run(main())
