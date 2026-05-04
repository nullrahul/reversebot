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
                markets = await self.gamma.fetch_ephemeral_crypto_markets(limit=5000)

                valid_markets = {}
                for m in markets:
                    mid = m["market_id"]
                    if self._should_track_market(m, now_override=int(true_now)):
                        valid_markets[mid] = m
                
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
                    logger.info("markets_locked", 
                                count=len(self.tracked_markets), 
                                sample=[m.get("question") for m in list(self.tracked_markets.values())[:12]])
                else:
                    logger.info("no_markets_locked", message="Searching for live 5m/15m windows...")

            except Exception as e:
                logger.error("discovery_error", error=str(e))
            await asyncio.sleep(self.config.short_window_discovery_interval_seconds if self.config.target_market_mode == "crypto_short_window" else 3600)

    def _should_track_market(self, market: Dict[str, Any], now_override: Optional[int] = None) -> bool:
        accepting = market.get("accepting_orders")
        if accepting is False: return False
            
        now = now_override if now_override is not None else int(time.time())
        if not market.get("active", True): return False

        q = market.get("question", "")
        # Regex matches 6:30AM-6:45AM
        match = re.search(r'(\d{1,2}(?::\d{2})?)\s*([APM]+)\s*-\s*(\d{1,2}(?::\d{2})?)\s*([APM]+)', q, re.I)
        if not match: return False

        import pytz
        et = pytz.timezone('US/Eastern')
        now_et = datetime.fromtimestamp(now, tz=pytz.utc).astimezone(et)
        
        # DATE CHECK DISABLED AS REQUESTED - ALLOWING "APRIL 19" MARKETS
        
        s_time, s_ampm, e_time, e_ampm = match.groups()
        if ":" not in s_time: s_time = f"{s_time}:00"
        if ":" not in e_time: e_time = f"{e_time}:00"
        
        # To calculate timestamps correctly, we use the date provided in the question or current date
        # If question contains "April 19", we use April 19 2026.
        year = 2026
        date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2})', q)
        if date_match:
            month_str, day_str = date_match.groups()
            date_prefix = f"{year} {month_str} {day_str}"
        else:
            date_prefix = now_et.strftime('%Y %B %d')

        try:
            s_dt = et.localize(datetime.strptime(f"{date_prefix} {s_time}{s_ampm}", '%Y %B %d %I:%M%p')).timestamp()
            e_dt = et.localize(datetime.strptime(f"{date_prefix} {e_time}{e_ampm}", '%Y %B %d %I:%M%p')).timestamp()
            
            duration = round((e_dt - s_dt) / 60)
            if duration not in [5, 15]: return False

            # Lock if it's currently trading (with 5m pre-flight)
            if (s_dt - 300) <= now <= e_dt:
                market["closes_at"] = e_dt
                return True
        except: pass
            
        return False

    async def run_scan_cycle(self) -> Tuple[int, int, List[Dict[str, Any]]]:
        scanned, flagged, confirmed = 0, 0, []
        now = int(await self.gamma.get_true_now())

        for mid, market in list(self.tracked_markets.items()):
            scanned += 1
            live_yes = self.ws.get_live_price(mid)
            if live_yes is None: live_yes = float(market.get("yes_price") or 0.0)
            
            # LIVE PRICE WATCHER LOG - FOR USER VERIFICATION
            logger.info("price_watch", q=market.get("question")[:40], p=live_yes)
            
            market["yes_price"] = live_yes
            market["no_price"] = round(1.0 - live_yes, 4)

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
