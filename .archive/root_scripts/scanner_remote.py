"""
Market Scanner — Poll & Threshold Detection for Polymarket Reverse Bot.
"""

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from config.config import BotConfig
from src.api.gamma_client import GammaClient
from src.api.websocket import WebSocketClient
from src.db.queries import (
    upsert_market,
    get_market,
    update_market_threshold_tracking,
    reset_market_threshold_tracking,
)
from src.logs.log_config import get_logger

logger = get_logger("scanner")

def _normalize_symbol(value: str) -> Optional[str]:
    lowered = value.lower()
    if any(x in lowered for x in ["bitcoin", "btc"]): return "BTC"
    if any(x in lowered for x in ["ethereum", "eth"]): return "ETH"
    return None

class MarketScanner:
    def __init__(self, config: BotConfig, gamma_client: GammaClient, ws_client: WebSocketClient, db):
        self.config = config
        self.gamma = gamma_client
        self.ws = ws_client
        self.db = db
        self.tracked_markets: Dict[str, dict] = {}
        self._discovery_task: Optional[asyncio.Task] = None
        self._should_run_discovery = False

    async def start_discovery(self):
        self._should_run_discovery = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        logger.info("discovery_started")

    async def stop_discovery(self):
        """Stop the background discovery task."""
        self._should_run_discovery = False
        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
        logger.info("discovery_stopped", message="Background market discovery stopped")

    async def _discovery_loop(self):
        while self._should_run_discovery:
            try:
                # Use search queries that actually surface the short-window markets
                queries = ["Bitcoin Price", "Ethereum Price", "Up or Down"]
                markets = []
                for q in queries:
                    batch = await self.gamma.fetch_active_markets(search=q, limit=100)
                    markets.extend(batch)
                
                # Also pull top volume markets as a fallback
                top_vol = await self.gamma.fetch_all_active_markets(max_pages=5)
                markets.extend(top_vol)

                valid_markets = {}
                for m in markets:
                    mid = m["market_id"]
                    if self._should_track_market(m):
                        valid_markets[mid] = m
                
                # Update subscriptions
                new_ids = set(valid_markets.keys()) - set(self.tracked_markets.keys())
                for mid in new_ids:
                    m = valid_markets[mid]
                    await self.ws.subscribe_market(mid, m.get("yes_token_id"), m.get("condition_id"), push_state=False)
                
                dead_ids = set(self.tracked_markets.keys()) - set(valid_markets.keys())
                for mid in dead_ids:
                    await self.ws.unsubscribe_market(mid, self.tracked_markets[mid].get("yes_token_id"), push_state=False)
                
                if new_ids or dead_ids:
                    await self.ws._push_subscription_state()
                
                self.tracked_markets = valid_markets
                if self.tracked_markets:
                    logger.info("markets_locked", count=len(self.tracked_markets), sample=[m.get("question")[:50] for m in list(self.tracked_markets.values())[:5]])

            except Exception as e:
                logger.error("discovery_error", error=str(e))
            await asyncio.sleep(self.config.short_window_discovery_interval_seconds if self.config.target_market_mode == "crypto_short_window" else 3600)

    def _should_track_market(self, market: Dict[str, Any]) -> bool:
        q = market.get("question", "").lower()
        
        # 1. Must be Bitcoin or Ethereum
        symbol = _normalize_symbol(q) or _normalize_symbol(market.get("slug", ""))
        if not symbol or symbol not in self.config.target_crypto_symbols:
            return False

        # 2. Must be a "Price" or "Up or Down" market
        is_price_type = any(x in q for x in ["price", "up or down", "above", "dip to"])
        if not is_price_type:
            return False

        # 3. Time Gate
        now = int(time.time())
        closes_at = market.get("closes_at")
        if not closes_at or closes_at <= (now + 60): # Already closed or closing in 1 min
            return False
            
        # Limit to markets closing within 24 hours for "short window"
        if (closes_at - now) > 86400:
            return False

        return True

    async def run_scan_cycle(self) -> Tuple[int, int, List[Dict[str, Any]]]:
        scanned, flagged, confirmed = 0, 0, []
        now = int(time.time())
        for mid, market in list(self.tracked_markets.items()):
            scanned += 1
            live_yes = self.ws.get_live_price(mid)
            if live_yes is None: live_yes = float(market.get("yes_price") or 0.0)
            
            market["yes_price"] = live_yes
            market["no_price"] = round(1.0 - live_yes, 4)

            if self.config.yes_bias_threshold <= live_yes < self.config.yes_bias_max_threshold:
                flagged += 1
                db_m = await get_market(self.db, mid)
                last_seen = (db_m.get("last_seen_above_threshold") or 0) if db_m else 0
                consecutive = (db_m.get("consecutive_cycles_above", 0) + 1) if db_m and (now - last_seen) < 90 else 1
                await update_market_threshold_tracking(self.db, mid, now, consecutive)
                if consecutive >= 2:
                    confirmed.append(market)
                    logger.info("signal_confirmed", q=market.get("question"), p=live_yes)
            else:
                await reset_market_threshold_tracking(self.db, mid)
            await upsert_market(self.db, market)
        return scanned, flagged, confirmed
