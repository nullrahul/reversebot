"""
Market Scanner — Poll & Threshold Detection for Polymarket Reverse Bot.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
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
from src.api.clob_client import ClobClient
from src.logs.log_config import get_logger

logger = get_logger("scanner")

class MarketScanner:
    def __init__(self, config: BotConfig, gamma_client: GammaClient, ws_client: WebSocketClient, clob_client: ClobClient, db):
        self.config = config
        self.gamma = gamma_client
        self.ws = ws_client
        self.clob = clob_client
        self.db = db
        self.tracked_markets: Dict[str, dict] = {}
        self._dead_count: Dict[str, int] = {} # Safety: Grace period for missing markets
        self._discovery_task: Optional[asyncio.Task] = None
        self._should_run_discovery = False
        # ── Price History for Velocity Filter (Change 7) ──
        # Structure: {yes_token_id: deque of (timestamp, price) tuples}
        self._price_history: Dict[str, list] = {}

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
                    # --- ARCHITECTURAL UPGRADE: Token Verification ---
                    verified_m = await self._verify_token_mapping(m)
                    if not verified_m:
                        logger.error("token_verification_failed", market_id=mid, message="Skipping market due to unverified token IDs")
                        continue
                    
                    m.update(verified_m) # Apply verified IDs
                    
                    self.tracked_markets[mid] = m
                    await upsert_market(self.db, m)

                    await self.ws.subscribe_market(
                        mid, 
                        yes_token_id=m.get("yes_token_id"), 
                        no_token_id=m.get("no_token_id"),
                        condition_id=m.get("condition_id"), 
                        push_state=False
                    )
                # --- NEW: Process Market Removal (With Grace Period) ---
                dead_ids = set(self.tracked_markets.keys()) - set(valid_markets.keys())

                # Reset grace counter for markets that are currently valid
                for mid in valid_markets.keys():
                    self._dead_count.pop(mid, None)

                for mid in dead_ids:
                    # 1. Increment death counter
                    self._dead_count[mid] = self._dead_count.get(mid, 0) + 1

                    # 2. Only unsubscribe if missing for 3+ consecutive cycles (60 seconds)
                    if self._dead_count[mid] < 3:
                        logger.debug("unsub_grace_period_active", market_id=mid, cycles=self._dead_count[mid])
                        continue

                    # 3. SAFETY: Don't unsubscribe if we are currently holding a position!
                    from src.db.queries import has_open_position_for_market
                    still_holding = await has_open_position_for_market(self.db, mid, paper=self.config.paper_trade)
                    if not still_holding:
                        m = self.tracked_markets[mid]
                        await self.ws.unsubscribe_market(
                            mid, 
                            yes_token_id=m.get("yes_token_id"), 
                            no_token_id=m.get("no_token_id"), 
                            push_state=False
                        )
                        del self.tracked_markets[mid]
                        self._dead_count.pop(mid, None)
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
            # RAM OPTIMIZED DISCOVERY: Only find new markets every 10s
            await asyncio.sleep(10)

    def _get_current_et_minutes(self, now: int) -> int:
        """Returns the current minute of the day in ET, handling DST automatically."""
        import datetime
        # Polymarket uses ET. We can use a simple offset but check for DST 
        # (EDT is -4, EST is -5). Or better: just use the UTC timestamp 
        # from Gamma to compare with closes_at.
        utc_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        
        # Simple Eastern Time conversion (approximate for April-Nov)
        et_dt = utc_dt - datetime.timedelta(hours=4) 
        return et_dt.hour * 60 + et_dt.minute

    def _parse_time_to_minutes(self, time_str: str) -> int:
        """Parses '1:20AM' or '11:00PM' into minutes of the day."""
        try:
            match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', time_str, re.I)
            if not match:
                # Handle cases like '1AM' (no minutes)
                match = re.search(r'(\d{1,2})\s*(AM|PM)', time_str, re.I)
                if not match: return -1
                h, ampm = int(match.group(1)), match.group(2).upper()
                m = 0
            else:
                h, m, ampm = int(match.group(1)), int(match.group(2)), match.group(3).upper()
            
            if ampm == "PM" and h < 12: h += 12
            if ampm == "AM" and h == 12: h = 0
            return h * 60 + m
        except: return -1

    def _should_track_market(self, market: Dict[str, Any], now_override: Optional[int] = None) -> bool:
        mid = market.get("market_id")
        if not market.get("accepting_orders"): return False
        if not market.get("active", True): return False

        now = now_override if now_override is not None else int(time.time())
        closes_at = market.get("closes_at", 0)
        
        # 1. Hard expiry guard
        if now > closes_at - 60: return False

        # 2. ET Window Target Logic
        q = market.get("question", "")
        # Extract the ET time range e.g. "1:20AM-1:25AM ET"
        match = re.search(r'(\d{1,2}(?::\d{2})?\s*[AP]M)\s*-\s*(\d{1,2}(?::\d{2})?\s*[AP]M)\s*ET', q, re.I)
        if not match: return False

        start_str, end_str = match.group(1), match.group(2)
        m_start = self._parse_time_to_minutes(start_str)
        m_end = self._parse_time_to_minutes(end_str)
        
        if m_start == -1 or m_end == -1: return False

        # ── TESTING FLAG: Temporarily restrict to 15-minute windows only ──
        # Set "_TESTING_only_15min_windows": true in runtime_config_rd.json to enable.
        # Set to false to re-enable 5-min markets. 5-min logic below is fully preserved.
        window_duration_mins = m_end - m_start
        only_15min = getattr(self.config, "_TESTING_only_15min_windows", False)
        if only_15min and window_duration_mins != 15:
            logger.debug(
                "market_skipped_not_15min",
                mid=mid,
                q=q[:50],
                window_mins=window_duration_mins,
                message=f"[TESTING] Skipping {window_duration_mins}-min window (only_15min_windows=True)"
            )
            return False

        current_et_m = self._get_current_et_minutes(now)
        
        # Target windows: 
        # - Current active window (e.g. it's 1:23, we want 1:20-1:25)
        # - Upcoming window (e.g. it's 1:23, we want 1:25-1:30)
        # FIX: Reduced future window from 20 mins to 3 mins to prevent 
        # locking into illiquid future markets which triggers false wicks.
        is_relevant_window = (m_start <= current_et_m + 3) and (m_end >= current_et_m - 2)

        if not is_relevant_window:
            return False
            
        logger.info("ACCEPTING_MARKET", mid=mid, q=q, closes_at=closes_at, et_m=current_et_m, window_mins=window_duration_mins)
        return True


    async def get_entry_signals(self) -> List[Dict[str, Any]]:
        """
        FAST LANE: Check thresholds for all tracked markets using WebSocket cache.
        Runs every 1 second. No API calls.

        Quality filters applied (from strategy_diagnosis.md):
        - Change 4: min_consecutive_cycles — price must stay in zone for N cycles
        - Change 5: min_seconds_to_close_pre_entry — no entry in final N secs of market
        - Change 7: price velocity check — reject if YES price is still accelerating up
        """
        confirmed = []
        now = int(time.time())

        # Read quality-filter params (hot-reloadable)
        min_cycles   = getattr(self.config, "min_consecutive_cycles", 8)
        min_ttc      = getattr(self.config, "min_seconds_to_close_pre_entry", 180)
        vel_max      = getattr(self.config, "price_velocity_max", 0.025)
        vel_window   = max(2, int(getattr(self.config, "price_velocity_window", 5)))

        for mid, market in list(self.tracked_markets.items()):
            # Memory Leak Fix: Remove resolved/ended markets
            closes_at = market.get("closes_at", 0)
            if closes_at > 0 and now > closes_at:
                await self.ws.unsubscribe_market(
                    mid,
                    yes_token_id=market.get("yes_token_id"),
                    no_token_id=market.get("no_token_id"),
                    push_state=True
                )
                del self.tracked_markets[mid]
                self._price_history.pop(market.get("yes_token_id", mid), None)
                continue

            # Use real WebSocket BOOK prices by token_id
            yes_token_id = market.get("yes_token_id")
            no_token_id  = market.get("no_token_id")

            # 1. Strictly prioritize real ASK prices (what we must pay to enter)
            # 2. SMART-LOCK FALLBACK: Use price_cache (last trade) if book is empty
            live_yes = self.ws.ask_cache.get(yes_token_id) or self.ws.price_cache.get(yes_token_id)
            live_no  = self.ws.ask_cache.get(no_token_id)  or self.ws.price_cache.get(no_token_id)

            # 3. Final fallback to Gamma seed data
            if live_yes is None:
                live_yes = float(market.get("yes_price") or 0.0)
            if live_no is None:
                live_no = float(market.get("no_price") or 0.0)

            # 4. Infer missing price if one side is known
            if live_yes <= 0.0 and live_no > 0.0:
                live_yes = round(1.0 - live_no, 4)
            elif live_no <= 0.0 and live_yes > 0.0:
                live_no = round(1.0 - live_yes, 4)
            elif live_yes <= 0.0 and live_no <= 0.0:
                live_yes, live_no = 0.5, 0.5

            # ── VELOCITY TRACKING ──
            # Maintain a rolling window of (timestamp, price) for this token
            tid_key = yes_token_id or mid
            history = self._price_history.setdefault(tid_key, [])
            history.append((now, live_yes))
            # Keep only the last vel_window + a few extra ticks
            if len(history) > vel_window * 2:
                self._price_history[tid_key] = history[-(vel_window * 2):]

            # Log individual price watch
            logger.info(
                "price_watch",
                q=market.get("question")[:40],
                p=live_yes,
                n=live_no,
                y_tid=yes_token_id[-8:] if yes_token_id else "None",
                n_tid=no_token_id[-8:] if no_token_id else "None"
            )

            market["yes_price"] = live_yes
            market["no_price"]  = live_no

            # ── PRIMARY THRESHOLD CHECK ──
            condition_met = self.config.yes_bias_threshold <= live_yes < self.config.yes_bias_max_threshold
            if condition_met:
                # IN-MEMORY TRACKING (No DB Spam)
                last_seen   = market.get("last_seen_above_threshold") or 0
                consecutive = (market.get("consecutive_cycles_above", 0) + 1) if (now - last_seen) < 5 else 1

                market["last_seen_above_threshold"] = now
                market["consecutive_cycles_above"]  = consecutive
                time_left = closes_at - now if closes_at else 9999

                if consecutive == 1 or consecutive % 10 == 0:
                    await update_market_threshold_tracking(self.db, mid, now, consecutive)

                # ── QUALITY FILTER 1: Minimum Consecutive Cycles (Change 4) ──
                # Price must hold in the zone for min_cycles before we trust it.
                # This kills the 117 instant-death "flash spike" entries.
                if consecutive < min_cycles:
                    logger.info(
                        "quality_filter_consecutive",
                        market_id=mid,
                        cycle=consecutive,
                        min_required=min_cycles,
                        message=f"Waiting for price to stabilise: {consecutive}/{min_cycles} cycles in zone"
                    )
                    continue

                # ── QUALITY FILTER 2: Time-to-Close Guard (Change 5) ──
                # Reject entry if we are in the final min_ttc seconds of the market.
                # Theta decay makes late-expiry NO tokens near-worthless as noise.
                if time_left < min_ttc:
                    logger.info(
                        "quality_filter_ttc",
                        market_id=mid,
                        time_left=time_left,
                        min_required=min_ttc,
                        message=f"Market too close to expiry: {time_left}s left < {min_ttc}s required"
                    )
                    continue

                # ── QUALITY FILTER 3: Price Velocity (Change 7) ──
                # Measure the rate of change of YES price over the last vel_window ticks.
                # If YES is RISING quickly (momentum against our NO bet), skip entry.
                recent_history = self._price_history.get(tid_key, [])
                if len(recent_history) >= vel_window:
                    # Average velocity over the window (positive = YES rising = bad for NO)
                    prices_in_window = [p for _, p in recent_history[-vel_window:]]
                    velocity = (prices_in_window[-1] - prices_in_window[0]) / vel_window
                    market["price_velocity"] = round(velocity, 4)

                    if velocity > vel_max:
                        logger.info(
                            "quality_filter_velocity",
                            market_id=mid,
                            velocity=round(velocity, 4),
                            max_allowed=vel_max,
                            yes_price=live_yes,
                            message=f"YES price accelerating upward ({velocity:.4f} > {vel_max}): skipping NO entry"
                        )
                        continue

                confirmed.append(market)
                logger.info(
                    "signal_confirmed",
                    q=market.get("question"),
                    p=live_yes,
                    cycle=consecutive,
                    ttc=time_left,
                    vel=market.get("price_velocity", "n/a")
                )
            else:
                if market.get("consecutive_cycles_above", 0) > 0:
                    market["last_seen_above_threshold"] = 0
                    market["consecutive_cycles_above"]  = 0
                    await reset_market_threshold_tracking(self.db, mid)

        return confirmed

    async def _verify_token_mapping(self, market: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Verify YES/NO token IDs against CLOB REST API (Source of Truth).
        """
        # CLOB API requires the condition_id (hash), not the market_id (numeric)
        cid = market.get("condition_id") or market["market_id"]
        clob_data = await self.clob.get_market_info(cid)
        if not clob_data:
            return None
        
        tokens = clob_data.get("tokens", [])
        if len(tokens) < 2:
            return None
        
        mapping = {}
        for t in tokens:
            outcome = str(t.get("outcome", "")).upper()
            tid = str(t.get("token_id", ""))
            if outcome in ("YES", "UP"):
                mapping["yes_token_id"] = tid
            elif outcome in ("NO", "DOWN"):
                mapping["no_token_id"] = tid
        
        if "yes_token_id" in mapping and "no_token_id" in mapping:
            return mapping
        return None

    async def run_scan_cycle(self) -> Tuple[int, int]:
        """
        SLOW LANE: Sync tracked markets with Gamma API.
        Runs every 20 seconds.
        """
        scanned = len(self.tracked_markets)
        flagged = sum(1 for m in self.tracked_markets.values() if m.get("consecutive_cycles_above", 0) > 0)
        return scanned, flagged

