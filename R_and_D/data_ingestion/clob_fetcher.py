import asyncio
import time
import os
import json
import subprocess
from typing import List, Dict, Any, Optional
from src.logs.log_config import get_logger
from config.config import load_config
from src.auth.l2_auth import L2Auth

logger = get_logger("rd_ingestion")

class ClobFetcher:
    def __init__(self, api_url: str = "https://clob.polymarket.com", gamma_url: str = "https://gamma-api.polymarket.com"):
        self.api_url = api_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        
        try:
            self.cfg = load_config()
            # USE THE EXACT ADDRESS THAT THE PROJECT'S L2AUTH USES
            # We found this 0x5f9a... address from the bot's initialization logs.
            # Using it here ensures the signature matches the API Key.
            self.auth = L2Auth(
                api_key=self.cfg.poly_api_key,
                api_secret=self.cfg.poly_api_secret,
                passphrase=self.cfg.poly_passphrase,
                address="0x5f9a5e172ec8cfc17cce7f9094b4ac37c34d502ccfcdd22b9786b4541872aff3"
            )
            logger.info(f"ClobFetcher ready using Bot's Verified Address: 0x5f9a...")
        except Exception as e:
            logger.error(f"Failed to load L2Auth: {e}")

    async def get_clob_token_ids(self, market_id: str) -> List[str]:
        url = f"{self.gamma_url}/markets/{market_id}"
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    tokens = data.get("clobTokenIds")
                    if isinstance(tokens, str):
                        try: tokens = json.loads(tokens)
                        except: pass
                    if isinstance(tokens, list):
                        return [str(t).strip(' "[]') for t in tokens]
            return []
        except Exception: return []

    def _get_curl_data(self, path: str) -> Optional[Dict]:
        headers = self.auth.build_headers("GET", path)
        cmd = ["curl", "-s", "-w", "\n%{http_code}", f"{self.api_url}{path}"]
        for k, v in headers.items():
            cmd.insert(2, "-H")
            cmd.insert(3, f"{k}: {v}")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                status_code = lines[-1]
                body = "\n".join(lines[:-1])
                if status_code == "200" and body.startswith(("{", "[")):
                    return json.loads(body)
                else:
                    logger.error(f"CLOB Error {status_code} for {path}: {body[:100]}")
            else:
                logger.error(f"Curl Error {result.returncode}")
        except Exception: pass
        return None

    async def fetch_price_history(self, market_id: str, interval: str = "1m") -> List[Dict]:
        await self.auth.sync_server_time()
        tokens = await self.get_clob_token_ids(market_id)
        if not tokens: return []

        for tid in reversed(tokens):
            path = f"/prices-history?fidelity=10&interval={interval}&market={tid}"
            data = self._get_curl_data(path)
            if data and (isinstance(data, list) or (isinstance(data, dict) and 'history' in data)):
                history = data.get("history", []) if isinstance(data, dict) else data
                if history:
                    logger.info(f"✅ Received {len(history)} points for {market_id}")
                    return [{"timestamp": i['t'], "price": float(i['p']), "market_id": market_id} for i in history]

        return await self.fetch_trades(market_id, reconstruct=True)

    async def fetch_trades(self, market_id: str, reconstruct: bool = False) -> List[Dict]:
        await self.auth.sync_server_time()
        tokens = await self.get_clob_token_ids(market_id)
        if not tokens: return []

        for tid in reversed(tokens):
            path = f"/trades?market={tid}"
            data = self._get_curl_data(path)
            if data:
                trades = data if isinstance(data, list) else data.get("trades", [])
                if not trades: continue
                if reconstruct:
                    temp_buckets = {}
                    for t in trades:
                        ts = int(t.get('timestamp', time.time()))
                        minute = ts // 60 * 60
                        if minute not in temp_buckets or ts > temp_buckets[minute]['ts']:
                            temp_buckets[minute] = {'ts': ts, 'price': float(t['price'])}
                    history = [{"timestamp": m, "price": v['price'], "market_id": market_id} for m, v in sorted(temp_buckets.items())]
                    if history:
                        logger.info(f"✅ Reconstructed {len(history)} points for {market_id}")
                    return history
                return trades
        return []
