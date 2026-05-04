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
                top_vol = await self.gamma.fetch_all_active_markets(max_pages=20)
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

    def _extract_window_minutes(self, q: str) -> Optional[int]:
        # Identify 5m or 15m intervals based on timestamps or explicit mentions
        match = re.search(r"\b(5|15)\s*(?:m|min|minute|minutes)\b|(?::(00|05|15|30|45))\s*(?:AM|PM)", q, re.I)
        if match:
            if match.group(1): return int(match.group(1))
            if match.group(2): return 15
        
        # Look for explicit 5m interval between timestamps like 8:25AM-8:30AM
        if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)\s*-\s*\d{1,2}:\d{2}\s*(?:am|pm)", q, re.I):
            # If it's a timestamp range, check if it's 5m or 15m. Usually "Up or Down" with a range are 5m or 15m.
            # Default to 5m if it looks like a short window and we can't parse exactly
            if "5m" in q.lower(): return 5
            if "15m" in q.lower(): return 15
            return 5 # Most "Up or Down" with time ranges are 5 min

        return None

    def _should_track_market(self, market: Dict[str, Any]) -> bool:
        if not market.get("accepting_orders", True):
            return False
            
        now = int(time.time())
        closes_at = market.get("closes_at")
        
        # 1. Time Gate: Must be closing soon, but not already closed (give at least 1 min to trade)
        if not closes_at or closes_at <= (now + 60): 
            return False
            
        time_to_close_hours = (closes_at - now) / 3600.0
        
        if time_to_close_hours < self.config.min_time_to_close_hours:
            return False
            
        if time_to_close_hours > (self.config.max_time_to_close_days * 24.0):
            return False
            
        # 2. Volume Gate (Relaxed for testing paper trades)
        # Polymarket sometimes reports 0 volume at the individual market level for grouped events
        # We will allow all markets that pass the time gate for paper trading tests.
            
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

            # Debug the threshold check
            if scanned % 20 == 0:  # Sample a few markets
                logger.info("threshold_eval_sample", 
                            market_id=mid, 
                            q=market.get("question", "")[:30], 
                            live_yes=live_yes, 
                            thresh=self.config.yes_bias_threshold,
                            max_thresh=self.config.yes_bias_max_threshold)

            if self.config.yes_bias_threshold <= live_yes < self.config.yes_bias_max_threshold:
                flagged += 1
                db_m = await get_market(self.db, mid)
                last_seen = (db_m.get("last_seen_above_threshold") or 0) if db_m else 0
                consecutive = (db_m.get("consecutive_cycles_above", 0) + 1) if db_m and (now - last_seen) < 90 else 1
                
                # UPDATE THE DICT BEFORE UPSERT SO IT DOESN'T GET OVERWRITTEN
                market["last_seen_above_threshold"] = now
                market["consecutive_cycles_above"] = consecutive
                
                await update_market_threshold_tracking(self.db, mid, now, consecutive)
                if consecutive >= 2:
                    confirmed.append(market)
                    logger.info("signal_confirmed", q=market.get("question"), p=live_yes)
                else:
                    logger.info("signal_first_cycle", q=market.get("question"), p=live_yes)
            else:
                market["last_seen_above_threshold"] = None
                market["consecutive_cycles_above"] = 0
                await reset_market_threshold_tracking(self.db, mid)
            await upsert_market(self.db, market)
        return scanned, flagged, confirmed
