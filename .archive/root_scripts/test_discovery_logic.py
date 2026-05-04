import asyncio
import math
import time
from typing import Optional, Dict, Any

# Mock class to test look-ahead logic
class MockGammaClient:
    def __init__(self):
        self._last_deep_discovery_ts = 0.0

    async def get_slugs(self):
        now_ts = time.time()
        base_ts = math.floor(now_ts / 300) * 300
        coins = ['btc', 'eth', 'sol', 'doge', 'bnb', 'xrp', 'hype']
        slugs_to_check = []
        
        # 1. Immediate Window: Next 10 mins (every cycle)
        for offset_mins in range(0, 15, 5):
            target_ts = int(base_ts + (offset_mins * 60))
            for c in coins:
                slugs_to_check.append(f'{c}-updown-5m-{target_ts}')

        # 2. 15m Boundary: Always check the next 15-minute boundary
        next_15m = math.ceil(now_ts / 900) * 900
        for c in coins:
            slugs_to_check.append(f'{c}-updown-15m-{next_15m}')

        # 3. Deep Discovery: Look ahead 45 mins (every 5 mins)
        deep_scan = False
        if now_ts - self._last_deep_discovery_ts > 300 or self._last_deep_discovery_ts == 0:
            deep_scan = True
            for offset_mins in range(15, 50, 5):
                target_ts = int(base_ts + (offset_mins * 60))
                for c in coins:
                    slugs_to_check.append(f'{c}-updown-5m-{target_ts}')
                    if target_ts % 900 == 0:
                        slugs_to_check.append(f'{c}-updown-15m-{target_ts}')
            self._last_deep_discovery_ts = now_ts
        
        return slugs_to_check, deep_scan

async def run_test():
    client = MockGammaClient()
    
    # Test 1: First scan (should be deep)
    slugs1, deep1 = await client.get_slugs()
    print(f"Scan 1 (Initial): Found {len(slugs1)} slugs | Deep Scan: {deep1}")
    has_15m = any("15m" in s for s in slugs1)
    print(f"  Includes 15m markets: {has_15m}")
    
    # Test 2: Immediate follow-up (should NOT be deep)
    slugs2, deep2 = await client.get_slugs()
    print(f"Scan 2 (Immediate): Found {len(slugs2)} slugs | Deep Scan: {deep2}")
    
    # Test 3: Check boundary alignment
    next_15m = math.ceil(time.time() / 900) * 900
    expected_15m = f"btc-updown-15m-{next_15m}"
    if expected_15m in slugs2:
        print(f"  SUCCESS: Next 15m boundary ({expected_15m}) found in shallow scan.")
    else:
        print(f"  FAILURE: Next 15m boundary MISSING in shallow scan.")

if __name__ == "__main__":
    asyncio.run(run_test())
