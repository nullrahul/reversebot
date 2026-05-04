import asyncio
from src.api.gamma_client import GammaClient
async def test():
    c = GammaClient()
    raw = await c._get('/markets', params={'limit': 1, 'search': 'Bitcoin', 'order': 'createdAt', 'ascending': 'false'})
    print('GOT RAW')
    try:
        parsed = c._parse_market(raw[0])
        print("PARSED:", bool(parsed))
    except Exception as e:
        import traceback; traceback.print_exc()
        print("ERROR:", e)
asyncio.run(test())
