"""
Market Scanner — Poll & Threshold Detection for Polymarket Reverse Bot.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta
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
        self._should_run_discovery = False
        if self._discovery_task:
            self._discovery_task.cancel()
            try: await self._discovery_task
            except asyncio.CancelledError: pass
        logger.info("discovery_stopped")

    async def _discovery_loop(self):
        while self._should_run_discovery:
            try:
                true_now = await self.gamma.get_true_now()
                markets = await self.gamma.fetch_ephemeral_crypto_markets(limit=200)

                valid_markets = {}
                for m in markets:
                    mid = m["market_id"]
                    if self._should_track_market(m, now_override=int(true_now)):
                        valid_markets[mid] = m
                
                new_ids = set(valid_markets.keys()) - set(self.tracked_markets.keys())
                for mid in new_ids:
                    m = valid_markets[mid]
                    await self.ws.subscribe_market(
                        mid, 
                        yes_token_id=m.get("yes_token_id"), 
                        no_token_id=m.get("no_token_id"),
                        condition_id=m.get("condition_id"), 
                        push_state=False
                    )
                
                dead_ids = set(self.tracked_markets.keys()) - set(valid_markets.keys())
                from src.db.queries import has_open_position_for_market
                for mid in dead_ids:
                    # SAFETY: Don't unsubscribe if we are currently holding a position!
                    # Otherwise PositionManager loses its price feed for TP/SL.
                    still_holding = await has_open_position_for_market(self.db, mid, paper=self.config.paper_trade)
                    if not still_holding:
                        await self.ws.unsubscribe_market(mid, self.tracked_markets[mid].get("yes_token_id"), push_state=False)
                    else:
                        logger.debug("unsub_prevented_active_pos", market_id=mid)
                
                if new_ids or dead_ids:
                    await self.ws._push_subscription_state()
                
                self.tracked_markets = valid_markets
                if self.tracked_markets:
                    logger.info("markets_locked", 
                                count=len(self.tracked_markets), 
                                sample=[m.get("question") for m in list(self.tracked_markets.values())[:12]])
                else:
                    logger.info("no_markets_locked", message="Searching for live 5m/15m windows...")

            except Exception as e:
                logger.error("discovery_error", error=str(e))
            # RAM OPTIMIZED DISCOVERY: Only find new markets every 60s
            await asyncio.sleep(60)

    def _should_track_market(self, market: Dict[str, Any], now_override: Optional[int] = None) -> bool:
        mid = market.get("market_id")
        accepting = market.get("accepting_orders")
        if accepting is False: 
            # logger.info("reject_accepting", mid=mid)
            return False
            
        now = now_override if now_override is not None else int(time.time())
        if not market.get("active", True): 
            # logger.info("reject_not_active", mid=mid)
            return False

        q = market.get("question", "")
        match = re.search(r'(\d{1,2}(?::\d{2})?).*?-.*(\d{1,2}(?::\d{2})?)', q)
        if not match: 
            # logger.info("reject_no_regex_match", mid=mid, q=q)
            return False

        closes_at = market.get("closes_at", 0)
        if now > closes_at - 90:
            # We stop tracking and unsubscribe 90s before close
            return False
            
        logger.info("ACCEPTING_MARKET", mid=mid, q=q, closes_at=closes_at)
        return True

    async def run_scan_cycle(self) -> Tuple[int, int, List[Dict[str, Any]]]:
        scanned, flagged, confirmed = 0, 0, []
        # Local time is perfectly fine for threshold logic, no need to hit Gamma API here
        now = int(time.time())

        for mid, market in list(self.tracked_markets.items()):
            scanned += 1
            
            # Use real WebSocket prices by token_id
            yes_token_id = market.get("yes_token_id")
            no_token_id = market.get("no_token_id")
            
            live_yes = self.ws.price_cache.get(yes_token_id) if yes_token_id else None
            live_no = self.ws.price_cache.get(no_token_id) if no_token_id else None
            
            # Fallback to existing Gamma data if WS is missing prices
            if live_yes is None: 
                live_yes = float(market.get("yes_price") or 0.5)
            
            if live_no is None:
                # If we have real Yes but not real No, synthesize complement
                live_no = round(1.0 - live_yes, 4)
            
            logger.info("price_watch", q=market.get("question")[:40], p=live_yes, n=live_no)
            
            market["yes_price"] = live_yes
            market["no_price"] = live_no

            condition_met = self.config.yes_bias_threshold <= live_yes < self.config.yes_bias_max_threshold
            if condition_met:
                flagged += 1
                db_m = await get_market(self.db, mid)
                last_seen = (db_m.get("last_seen_above_threshold") or 0) if db_m else 0
                consecutive = (db_m.get("consecutive_cycles_above", 0) + 1) if db_m and (now - last_seen) < 90 else 1
                
                market["last_seen_above_threshold"] = now
                market["consecutive_cycles_above"] = consecutive
                await update_market_threshold_tracking(self.db, mid, now, consecutive)
                
                if consecutive >= (1 if self.config.target_market_mode == "crypto_short_window" else 2):
                    confirmed.append(market)
                    logger.info("signal_confirmed", q=market.get("question"), p=live_yes)
            else:
                market["last_seen_above_threshold"] = None
                market["consecutive_cycles_above"] = 0
                await reset_market_threshold_tracking(self.db, mid)
            await upsert_market(self.db, market)
        return scanned, flagged, confirmed
