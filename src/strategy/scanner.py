"""
Market Scanner — Streak Momentum Poll & Discovery for Polymarket Reverse Bot.
"""

import asyncio
import math
import re
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from config.config import BotConfig
from src.api.gamma_client import GammaClient
from src.api.websocket import WebSocketClient
from src.db.queries import (
    upsert_market,
    get_market,
)
from src.api.clob_client import ClobClient
from src.logs.log_config import get_logger
from src.recorder.state_exporter import StateExporter

logger = get_logger("scanner")

class MarketScanner:
    def __init__(self, config: BotConfig, gamma_client: GammaClient, ws_client: WebSocketClient, clob_client: ClobClient, db):
        self.config = config
        self.gamma = gamma_client
        self.ws = ws_client
        self.clob = clob_client
        self.db = db
        self.state_exporter = StateExporter(config)
        self.tracked_markets: Dict[str, dict] = {}
        self._ws_subscribed_markets: Dict[str, dict] = {}
        self._dead_count: Dict[str, int] = {}  # Safety: Grace period for missing markets
        self._discovery_task: Optional[asyncio.Task] = None
        self._should_run_discovery = False
        
        # Outcome cache (slug -> 'UP', 'DOWN', or '?')
        self._outcome_cache: Dict[str, str] = {}

        # Streak reset tracking registry: (symbol, window_minutes) -> last_traded_closes_at
        self.last_traded_close_ts: Dict[Tuple[str, int], int] = {}
        self.scan_count = 0
        self._last_latency_check = 0.0
        self.last_api_latency_ms = 0.0
        self._last_spread_log_time: Dict[str, float] = {}
        # Post-entry spread recording registry: market_id -> record_until_timestamp
        self._spread_recording_windows: Dict[str, float] = {}

        # Telemetry Expansion Deques (Phase 28 Engine)
        self._spot_ticks: Dict[str, deque] = {
            "BTC": deque(maxlen=3600),
            "ETH": deque(maxlen=3600),
            "SOL": deque(maxlen=3600)
        }
        self._imbalance_deques: Dict[str, deque] = {}

    def record_post_entry_spread(self, market_id: str, duration_seconds: float = 5.0) -> None:
        """Activate spread logging for this market for the next N seconds after a position is opened."""
        if market_id:
            now = time.time()
            self._spread_recording_windows[str(market_id)] = now + duration_seconds
            if len(self._spread_recording_windows) > 50:
                self._spread_recording_windows = {
                    m: exp for m, exp in self._spread_recording_windows.items() if exp > now
                }

    def _update_spot_tick(self, symbol: str, price: float, timestamp: float) -> None:
        if price > 0.0 and symbol in self._spot_ticks:
            self._spot_ticks[symbol].append((timestamp, price))

    def _get_realized_volatility(self, symbol: str, duration_seconds: int) -> float:
        if symbol not in self._spot_ticks or len(self._spot_ticks[symbol]) < 2:
            return 0.001
        now = time.time()
        cutoff = now - duration_seconds
        prices = [p for t, p in self._spot_ticks[symbol] if t >= cutoff]
        if len(prices) < 2:
            return 0.001
        
        log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices)) if prices[i-1] > 0]
        if not log_returns:
            return 0.001
        
        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
        return math.sqrt(variance)

    def _get_spot_velocity_10s(self, symbol: str) -> float:
        if symbol not in self._spot_ticks or len(self._spot_ticks[symbol]) < 2:
            return 0.0
        now = time.time()
        cutoff = now - 10.0
        ticks = [p for t, p in self._spot_ticks[symbol] if t >= cutoff]
        if len(ticks) < 2 or ticks[0] <= 0:
            return 0.0
        return ((ticks[-1] - ticks[0]) / ticks[0]) * 10000.0

    def _compute_orderbook_depths(self, book: dict, best_ask: float, best_bid: float) -> Tuple[float, float, float, float]:
        depth_1c = 0.0
        depth_5c = 0.0
        ask_depth_1c = 0.0
        bid_depth_1c = 0.0
        
        if not book:
            return 0.0, 0.0, 0.0, 0.0
            
        asks = book.get("asks", {})
        bids = book.get("bids", {})
        
        if asks and best_ask > 0.0:
            for p, s in asks.items():
                price = float(p)
                size = float(s)
                if price <= best_ask + 0.01:
                    depth_1c += price * size
                    ask_depth_1c += price * size
                if price <= best_ask + 0.05:
                    depth_5c += price * size
                    
        if bids and best_bid > 0.0:
            for p, s in bids.items():
                price = float(p)
                size = float(s)
                if price >= best_bid - 0.01:
                    bid_depth_1c += price * size
                    
        return round(depth_1c, 2), round(depth_5c, 2), round(ask_depth_1c, 2), round(bid_depth_1c, 2)


    def _get_now(self) -> int:
        try:
            t = self.clob.get_synced_time()
            if hasattr(t, "__await__") or not isinstance(t, (int, float)):
                return int(time.time())
            return int(t)
        except Exception:
            return int(time.time())

    def _get_market_key_info(self, market: dict) -> Tuple[str, int]:
        """
        Extract the symbol (e.g. 'BTC', 'ETH', 'SOL') and window size in minutes (e.g. 5, 15) from a market.
        """
        slug = (market.get("slug") or "").lower()
        question = (market.get("question") or "").lower()
        
        # 1. Parse window minutes
        window = market.get("window_minutes")
        if not window:
            if "-5m-" in slug or "5-minute" in question:
                window = 5
            elif "-15m-" in slug or "15-minute" in question:
                window = 15
            else:
                window = 5 # default fallback
                
        # 2. Parse symbol
        symbol = "BTC"
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or re.search(r'\bsol\b', question) is not None:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"
        else:
            # Fallback to checking target config
            for target in self.config.target_crypto_symbols:
                if target.lower() in question or target.lower() in slug:
                    symbol = target.upper()
                    break
                    
        return symbol, window

    async def _init_last_traded_close_times(self):
        """
        Query the database for all previously traded markets and initialize the
        last_traded_close_ts mapping.
        """
        from src.db.queries import get_last_traded_market_close_times
        try:
            traded_markets = await get_last_traded_market_close_times(self.db, paper=self.config.paper_trade)
            self.last_traded_close_ts = {}
            for row in traded_markets:
                q = row.get("question") or ""
                slug = row.get("slug") or ""
                closes_at = row.get("closes_at")
                if closes_at:
                    closes_at = int(closes_at)
                    temp_m = {"question": q, "slug": slug}
                    symbol, window = self._get_market_key_info(temp_m)
                    key = (symbol, window)
                    if key not in self.last_traded_close_ts or closes_at > self.last_traded_close_ts[key]:
                        self.last_traded_close_ts[key] = closes_at
            logger.info("initialized_last_traded_close_times", mapping=str(self.last_traded_close_ts))
        except Exception as e:
            logger.error("failed_to_init_last_traded_close_times", error=str(e))
            self.last_traded_close_ts = {}

    async def start_discovery(self):
        await self._init_last_traded_close_times()
        self._should_run_discovery = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        logger.info("discovery_started")

    async def stop_discovery(self):
        self._should_run_discovery = False
        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
        logger.info("discovery_stopped")

    async def _discovery_loop(self):
        while self._should_run_discovery:
            try:
                true_now = await self.gamma.get_true_now()
                markets = await self.gamma.fetch_ephemeral_crypto_markets()

                valid_markets = {}
                for m in markets:
                    mid = m["market_id"]
                    if self._should_track_market(m, now_override=int(true_now)):
                        valid_markets[mid] = m
                
                new_ids = set(valid_markets.keys()) - set(self.tracked_markets.keys())
                btc_price_open = None
                if new_ids:
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    btc_price_open = float(data.get("price", 0.0))
                                    logger.info("fetched_btc_price_for_new_markets", btc_price=btc_price_open)
                    except Exception as e:
                        logger.error("failed_to_fetch_btc_price_for_new_markets", error=str(e) or type(e).__name__)

                for mid, existing_m in self.tracked_markets.items():
                    if mid not in self._ws_subscribed_markets:
                        self._ws_subscribed_markets[mid] = dict(existing_m)

                for mid in new_ids:
                    m = valid_markets[mid]
                    
                    # Verify Token Mapping
                    verified_m = await self._verify_token_mapping(m)
                    if not verified_m:
                        logger.error("token_verification_failed", market_id=mid, message="Skipping market due to unverified token IDs")
                        continue
                    
                    m.update(verified_m)  # Apply verified IDs
                    if btc_price_open:
                        m["btc_price_at_market_open"] = btc_price_open
                    
                    self.tracked_markets[mid] = m
                    self._ws_subscribed_markets[mid] = dict(m)
                    try:
                        await upsert_market(self.db, m)
                    except Exception as db_err:
                        logger.warning("market_upsert_delayed", market_id=mid, error=str(db_err))

                    await self.ws.subscribe_market(
                        mid, 
                        yes_token_id=m.get("yes_token_id"), 
                        no_token_id=m.get("no_token_id"),
                        condition_id=m.get("condition_id"), 
                        push_state=False
                    )
                        
                # Process Market Removal (With Grace Period)
                ws_sub_keys = (
                    set(self.ws._subscribed_markets.keys())
                    if isinstance(getattr(self.ws, "_subscribed_markets", None), dict)
                    else set()
                )
                dead_ids = (
                    set(self.tracked_markets.keys())
                    | set(self._ws_subscribed_markets.keys())
                    | ws_sub_keys
                ) - set(valid_markets.keys())

                # Reset grace counter for markets that are currently valid
                for mid in valid_markets.keys():
                    self._dead_count.pop(mid, None)

                for mid in dead_ids:
                    m = self._ws_subscribed_markets.get(mid) or self.tracked_markets.get(mid)
                    closes_at = int(m.get("closes_at", 0) or 0) if m else 0
                    if closes_at == 0:
                        try:
                            db_m = await get_market(self.db, mid)
                            if db_m and isinstance(db_m, dict) and db_m.get("closes_at"):
                                closes_at = int(db_m["closes_at"])
                                if m is None:
                                    m = db_m
                                    self._ws_subscribed_markets[mid] = dict(db_m)
                        except Exception:
                            pass

                    # Increment death counter
                    self._dead_count[mid] = self._dead_count.get(mid, 0) + 1

                    # Only unsubscribe if missing for 3+ consecutive cycles (30 seconds)
                    # AND after the candle's closes_at has passed so open positions keep live WS ticks until expiry.
                    if self._dead_count[mid] < 3 or (closes_at > 0 and int(true_now) <= closes_at):
                        logger.debug("unsub_grace_period_active", market_id=mid, cycles=self._dead_count[mid])
                        continue

                    # SAFETY: Don't unsubscribe if we are currently holding an active un-expired position!
                    from src.db.queries import has_open_position_for_market
                    still_holding = await has_open_position_for_market(self.db, mid, paper=self.config.paper_trade)
                    if not still_holding or (closes_at > 0 and int(true_now) > closes_at + 120):
                        await self.ws.unsubscribe_market(
                            mid, 
                            yes_token_id=m.get("yes_token_id") if m else None, 
                            no_token_id=m.get("no_token_id") if m else None, 
                            push_state=False
                        )
                        self.tracked_markets.pop(mid, None)
                        self._ws_subscribed_markets.pop(mid, None)
                        self._dead_count.pop(mid, None)
                        self._imbalance_deques.pop(mid, None)
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
                logger.error("discovery_error", error=str(e) or type(e).__name__)
            # RAM OPTIMIZED DISCOVERY: Only find new markets every 10s
            await asyncio.sleep(10)

    def _should_track_market(self, market: Dict[str, Any], now_override: Optional[int] = None) -> bool:
        mid = market.get("market_id")
        if not market.get("accepting_orders"): return False
        if not market.get("active", True): return False

        now = now_override if now_override is not None else self._get_now()
        closes_at = market.get("closes_at", 0)
        
        # Hard expiry guard (at least 60 seconds remaining to allow fast entries)
        if now > closes_at - 60: return False

        # Only track configured target crypto symbols micro-markets (5m, 15m)
        q = market.get("question", "")
        if "Up or Down" not in q: return False

        symbol_map = {
            "BTC": ["BITCOIN", "BTC"],
            "ETH": ["ETHEREUM", "ETH"],
            "SOL": ["SOLANA", "SOL"],
        }
        
        matched_any = False
        q_upper = q.upper()
        for sym in self.config.target_crypto_symbols:
            sym_upper = sym.upper()
            terms = symbol_map.get(sym_upper, [sym_upper])
            if any(term in q_upper for term in terms):
                matched_any = True
                break

        if not matched_any:
            return False

        win_mins = market.get("window_minutes")
        if win_mins not in (5, 15):
            return False

        logger.info("ACCEPTING_MARKET", mid=mid, q=q, closes_at=closes_at)
        return True

    async def _get_binance_candle_data(self, coin: str, duration: int, start_time: int) -> Tuple[float, float, str, float]:
        """
        Fetch kline from Binance and calculate open, close, outcome, and delta_bps.
        Returns: Tuple[open_price, close_price, outcome, delta_bps]
        """
        symbol = f"{coin.upper()}USDT"
        interval = f"{duration}m"
        start_time_ms = start_time * 1000
        
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_time_ms}&limit=1"
        logger.info("fetching_binance_candle_data", symbol=symbol, interval=interval, start_time=start_time)
        try:
            session = await self.gamma._ensure_session()
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    klines = await resp.json()
                    if klines and len(klines) > 0:
                        k = klines[0]
                        returned_start = int(k[0] // 1000)
                        if abs(returned_start - start_time) > 10:
                            logger.warning("binance_timestamp_mismatch", requested=start_time, returned=returned_start)
                            return 0.0, 0.0, "?", 0.0
                            
                        open_price = float(k[1])
                        close_price = float(k[4])
                        outcome = "UP" if close_price > open_price else "DOWN"
                        delta_bps = 0.0
                        if open_price > 0:
                            delta_bps = ((close_price - open_price) / open_price) * 10000.0
                        logger.info("binance_candle_resolved", symbol=symbol, start_time=start_time, outcome=outcome, delta_bps=delta_bps)
                        return open_price, close_price, outcome, delta_bps
                logger.warning("binance_api_candle_error", status=resp.status, url=url)
        except Exception as e:
            logger.error("binance_candle_fetch_failed", symbol=symbol, start_time=start_time, error=str(e) or type(e).__name__)
        return 0.0, 0.0, "?", 0.0

    async def _get_binance_outcome(self, coin: str, duration: int, start_time: int) -> str:
        """
        Fetch the outcome of a completed candle from the public Binance Klines API.
        Returns: 'UP', 'DOWN', or '?'
        """
        # Safety check: Is the candle fully completed?
        now = self._get_now()
        if now < start_time + duration * 60:
            logger.debug("binance_candle_not_yet_completed", coin=coin, start_time=start_time)
            return "?"

        _, _, outcome, _ = await self._get_binance_candle_data(coin, duration, start_time)
        return outcome

    async def _fetch_single_outcome(self, slug: str, t_start: int, coin: str, duration: int) -> dict:
        if slug in self._outcome_cache and isinstance(self._outcome_cache[slug], dict):
            return self._outcome_cache[slug]
            
        logger.debug("fetching_historical_outcome", slug=slug)
        outcome = "?"
        source = "GAMMA"
        delta_bps = 0.0
        
        try:
            # 1. Try Gamma first
            data = await self.gamma._get("/events", params={"slug": slug})
            if data and isinstance(data, list) and len(data) > 0:
                markets = data[0].get("markets", [])
                if markets and len(markets) > 0:
                    m = markets[0]
                    closed = m.get("closed", False)
                    if closed:
                        op = m.get("outcomePrices")
                        if op:
                            import json
                            op_list = json.loads(op) if isinstance(op, str) else op
                            if op_list and len(op_list) > 0:
                                yes_final = float(op_list[0])
                                outcome = "UP" if yes_final > 0.5 else "DOWN"
                                source = "GAMMA"
                                # Fetch Binance candle details to log delta_bps even if Gamma resolved
                                _, _, _, delta_bps = await self._get_binance_candle_data(coin, duration, t_start)
                                
                                val = {"outcome": outcome, "source": source, "delta_bps": delta_bps}
                                self._outcome_cache[slug] = val
                                return val
            
            # 2. Fall back to Binance if Gamma unresolved
            logger.info("gamma_outcome_missing_falling_back_to_binance", slug=slug, start_time=t_start)
            _, _, b_outcome, b_delta = await self._get_binance_candle_data(coin, duration, t_start)
            if b_outcome != "?":
                outcome = b_outcome
                source = "BINANCE_FALLBACK"
                delta_bps = b_delta
                
                val = {"outcome": outcome, "source": source, "delta_bps": delta_bps}
                self._outcome_cache[slug] = val
                return val
            else:
                return {"outcome": "?", "source": "UNKNOWN", "delta_bps": 0.0}
        except Exception as e:
            logger.warning("failed_fetching_historical_outcome", slug=slug, error=str(e) or type(e).__name__)
            return {"outcome": "?", "source": "ERROR", "delta_bps": 0.0}

    async def _get_historical_outcomes(self, asset: str, duration: int, closes_at: int) -> dict:
        """
        Derive slugs for the last 10 completed markets and fetch their outcomes + deltas in parallel.
        Returns: Dict containing out1..out30, sources, deltas, and combined source.
        """
        dur_secs = duration * 60
        # Generate timestamps for t1..t10
        timestamps = [closes_at - (i * dur_secs) for i in range(2, 12)]
        
        asset_lower = asset.lower()
        if 'bitcoin' in asset_lower or 'btc' in asset_lower:
            coin = 'btc'
        elif 'ethereum' in asset_lower or 'eth' in asset_lower:
            coin = 'eth'
        else:
            empty_res = {
                "source": "UNKNOWN"
            }
            for idx in range(1, 31):
                empty_res[f"out{idx}"] = "?"
                empty_res[f"src{idx}"] = "UNKNOWN"
                empty_res[f"delta{idx}"] = 0.0
            return empty_res
            
        slugs = [f"{coin}-updown-{duration}m-{t}" for t in timestamps]
        
        # Parallelize requests using asyncio.gather
        tasks = [self._fetch_single_outcome(slug, t_start, coin, duration) for slug, t_start in zip(slugs, timestamps)]
        results = await asyncio.gather(*tasks)
        
        # Manage cache size
        if len(self._outcome_cache) > 200:
            try:
                # Evict oldest 50 items
                keys_to_evict = list(self._outcome_cache.keys())[:50]
                for k in keys_to_evict:
                    self._outcome_cache.pop(k, None)
                logger.debug("outcome_cache_pruned", evicted_count=len(keys_to_evict))
            except Exception as cache_err:
                logger.warning("failed_to_prune_outcome_cache", error=str(cache_err))
                
        combined_source = "GAMMA"
        if any(r["source"] == "BINANCE_FALLBACK" for r in results):
            combined_source = "BINANCE_FALLBACK"
        elif any(r["source"] == "UNKNOWN" for r in results):
            combined_source = "UNKNOWN"
            
        ret_val = {
            "source": combined_source
        }
        for idx, res in enumerate(results):
            ret_val[f"out{idx+1}"] = res["outcome"]
            ret_val[f"src{idx+1}"] = res["source"]
            ret_val[f"delta{idx+1}"] = res["delta_bps"]
            
        # Fallback fields for out11..out30 to avoid any potential KeyErrors downstream
        for idx in range(10, 30):
            ret_val[f"out{idx+1}"] = "?"
            ret_val[f"src{idx+1}"] = "UNKNOWN"
            ret_val[f"delta{idx+1}"] = 0.0
            
        return ret_val

    async def get_entry_signals(self) -> List[Dict[str, Any]]:
        """
        Check for Streak Momentum entry signals across all tracked markets using WebSocket cache.
        Checks if the last 2 completed micro-markets resolved to the same side (UP-UP or DOWN-DOWN).
        """
        if hasattr(self, "state_exporter"):
            try:
                await self.state_exporter.export_state_snapshot(self, binance_ws=getattr(self, "binance_ws", None))
            except Exception as e:
                logger.error("state_exporter_call_failed", error=str(e))

        confirmed = []
        now = self._get_now()

        for mid, market in list(self.tracked_markets.items()):
            closes_at = market.get("closes_at", 0)
            if closes_at > 0 and now > closes_at:
                await self.ws.unsubscribe_market(
                    mid,
                    yes_token_id=market.get("yes_token_id"),
                    no_token_id=market.get("no_token_id"),
                    push_state=True
                )
                self.tracked_markets.pop(mid, None)
                continue
            duration = market.get("window_minutes")
            if duration not in (5, 15):
                continue

            symbol, window = self._get_market_key_info(market)
            
            # --- TICK-BY-TICK SPREAD LOGGING FOR QUANT ANALYSIS ---
            # Retrieve real-time L2 best bid and ask from the WebSocket client
            yes_tid = market.get("yes_token_id")
            no_tid = market.get("no_token_id")
            yes_slippage = 0.0
            no_slippage = 0.0
            if yes_tid and no_tid:
                yes_bid = self.ws.bid_cache.get(yes_tid)
                yes_ask = self.ws.ask_cache.get(yes_tid)
                no_bid = self.ws.bid_cache.get(no_tid)
                no_ask = self.ws.ask_cache.get(no_tid)

                
                # We analyze bid-ask spread on the YES side (or counterpart NO side)
                # Let's log if at least YES or NO best levels are cached
                best_bid = yes_bid if yes_bid is not None else (round(1.0 - no_ask, 4) if no_ask is not None else None)
                best_ask = yes_ask if yes_ask is not None else (round(1.0 - no_bid, 4) if no_bid is not None else None)
                
                if best_bid is not None and best_ask is not None:
                    spread_amount = best_ask - best_bid
                    spread_pct = spread_amount / best_ask if best_ask > 0 else 0.0
                    
                    # Calculate FAK sweep in-memory using WS book_cache
                    budget = float(self.config.position_size_usdc)
                    
                    # 1. YES sweep
                    yes_book = self.ws.book_cache.get(yes_tid) if yes_tid else None
                    yes_asks = yes_book.get("asks", {}) if yes_book else {}
                    yes_fak_price, yes_slippage, yes_fillable = self._simulate_fak_sweep(yes_asks, budget)
                    
                    # 2. NO sweep
                    no_book = self.ws.book_cache.get(no_tid) if no_tid else None
                    no_asks = no_book.get("asks", {}) if no_book else {}
                    no_fak_price, no_slippage, no_fillable = self._simulate_fak_sweep(no_asks, budget)
                    
                    # 3. Latency measurement (throttled background task)
                    now_time = self._get_now()
                    if now_time - self._last_latency_check >= 5.0:
                        self._last_latency_check = now_time
                        asyncio.create_task(self._measure_api_latency())
                    
                    # Store tick in the high-performance spread_logs table ONLY during active post-entry recording window
                    # (throttled to at most once per 1 second per market for 5s post-entry)
                    record_until = self._spread_recording_windows.get(mid, 0.0)
                    if record_until > 0.0 and now_time <= record_until:
                        if now_time - self._last_spread_log_time.get(mid, 0.0) >= 1.0:
                            self._last_spread_log_time[mid] = float(now_time)
                            from src.db.queries import log_spread_tick
                            asyncio.create_task(log_spread_tick(
                                self.db,
                                market_id=mid,
                                symbol=symbol,
                                window_minutes=window,
                                best_bid=float(best_bid),
                                best_ask=float(best_ask),
                                spread_amount=float(spread_amount),
                                spread_pct=float(spread_pct),
                                sim_fok_yes_price=float(yes_fak_price),
                                sim_fok_yes_slippage=float(yes_slippage),
                                sim_fok_yes_fillable=int(yes_fillable),
                                sim_fok_no_price=float(no_fak_price),
                                sim_fok_no_slippage=float(no_slippage),
                                sim_fok_no_fillable=int(no_fillable),
                                api_latency_ms=float(self.last_api_latency_ms)
                            ))
                    elif mid in self._spread_recording_windows:
                        self._spread_recording_windows.pop(mid, None)

            # Only check outcomes/signals once the market's active trading window has started.
            # Prevents polling historical outcomes for future markets that haven't reached their active candle yet.
            start_time = closes_at - (duration * 60)
            if now < start_time:
                continue
            dur_secs = duration * 60
            q = market.get("question", "")
            asset = "BTC" if ("Bitcoin" in q or "BTC" in q) else "ETH"

            # 1. Fetch historical outcomes
            res = await self._get_historical_outcomes(asset, duration, closes_at)
            out1 = res["out1"]
            out2 = res["out2"]
            out3 = res["out3"]
            
            if out1 == "?" or out2 == "?":
                # Outcomes not yet resolved (waiting for Chainlink settlement)
                logger.debug("historical_outcomes_lagging", market_id=mid, slug=market.get("slug"), out1=out1, out2=out2)
                continue
                
            # 2. Streak Momentum Match
            if out1 != out2:
                # Oscillating/choppy market — stay out of main pipeline
                # Emit a lightweight signal for claude_every virtual profile (streak = 1)
                target_side = "YES" if out1 == "UP" else "NO"
                yes_token_id = market.get("yes_token_id")
                no_token_id  = market.get("no_token_id")
                
                live_yes = self.ws.get_best_ask(yes_token_id)
                live_no  = self.ws.get_best_ask(no_token_id)
                
                if live_yes is None:
                    live_yes = float(market.get("yes_price") or 0.5)
                if live_no is None:
                    live_no = float(market.get("no_price") or 0.5)
                    
                if live_yes <= 0.0 and live_no > 0.0:
                    live_yes = round(1.0 - live_no, 4)
                elif live_no <= 0.0 and live_yes > 0.0:
                    live_no = round(1.0 - live_yes, 4)
                    
                target_price = live_yes if target_side == "YES" else live_no
                target_token_id = yes_token_id if target_side == "YES" else no_token_id
                
                lightweight_payload = market.copy()
                lightweight_payload["yes_price"] = live_yes
                lightweight_payload["no_price"] = live_no
                lightweight_payload["target_side"] = target_side
                lightweight_payload["target_token_id"] = target_token_id
                lightweight_payload["target_price"] = target_price
                lightweight_payload["streak_depth"] = 1
                lightweight_payload["is_lightweight"] = True
                lightweight_payload["streak_quality_score"] = 0.0
                
                confirmed.append(lightweight_payload)
                logger.info(
                    "lightweight_signal_confirmed",
                    market_id=mid,
                    q=q,
                    target_side=target_side,
                    target_price=target_price,
                    message="Streak = 1. Emitting lightweight signal for claude_every profile."
                )
                continue

            # Determine dynamic streak depth (dynamically check up to 30 outcomes)
            streak_depth = 2
            for d in range(3, 31):
                all_equal = True
                for i in range(1, d + 1):
                    val_i = res.get(f"out{i}")
                    if val_i != out1 or val_i == "?":
                        all_equal = False
                        break
                if all_equal:
                    streak_depth = d
                else:
                    break

            # Calculate Streak Quality Score (passive filter for volatility/choppiness)
            # FIXED (v3.103): Start evaluating transitions strictly BEFORE the current streak
            # to prevent the triggering streak (e.g., out1 == out2) from biasing the score upward.
            window_val = getattr(self.config, "streak_quality_window", 7)
            window_size = int(window_val) if isinstance(window_val, (int, float)) else 7
            same_count = 0
            valid_transitions = 0
            # Start from streak_depth so we evaluate out2 vs out3, out3 vs out4, etc.
            # We cap at 10 because we now fetch up to out10.
            for i in range(streak_depth, min(streak_depth + window_size, 10)):
                o_curr = res.get(f"out{i}")
                o_prev = res.get(f"out{i+1}")
                if o_curr and o_prev and o_curr != "?" and o_prev != "?":
                    valid_transitions += 1
                    if o_curr == o_prev:
                        same_count += 1
            # Adjust required transitions dynamically: if we have deep streaks, we have fewer outcomes left to check.
            required_transitions = max(1, min(4, 10 - streak_depth))
            streak_quality_score = (same_count / valid_transitions) if valid_transitions >= required_transitions else None

            # Enforce Streak Reset Guard: Ensure both completed markets used for the streak
            # closed strictly after the close time of the last traded market.
            # Bypassed for Mean Reversion scaling when streak_depth >= 3 (continuous fade)
            if streak_depth < 3:
                key = (symbol, window)
                last_traded = self.last_traded_close_ts.get(key, 0)
                dur_secs = duration * 60
                m_prev2_close = closes_at - (2 * dur_secs)
                if m_prev2_close < last_traded:
                    logger.debug(
                        "streak_reset_guard_active",
                        market_id=mid,
                        symbol=symbol,
                        window=window,
                        m_prev2_close=m_prev2_close,
                        last_traded=last_traded,
                        message="Streak reset: waiting for completely fresh resolved markets."
                    )
                    continue

            # Pure Trend-Following Strategy (v3.79): Always trade with the trend direction
            # If the streak is UP, we buy YES. If the streak is DOWN, we buy NO.
            target_side = "YES" if out1 == "UP" else "NO"
            
            yes_token_id = market.get("yes_token_id")
            no_token_id  = market.get("no_token_id")
            live_yes = self.ws.get_best_ask(yes_token_id)
            live_no  = self.ws.get_best_ask(no_token_id)

            if live_yes is None:
                live_yes = float(market.get("yes_price") or 0.5)
            if live_no is None:
                live_no = float(market.get("no_price") or 0.5)

            if live_yes <= 0.0 and live_no > 0.0:
                live_yes = round(1.0 - live_no, 4)
            elif live_no <= 0.0 and live_yes > 0.0:
                live_no = round(1.0 - live_yes, 4)

            market["yes_price"] = live_yes
            market["no_price"]  = live_no
            
            # Determine correct price and token ID for execution
            target_price = live_yes if target_side == "YES" else live_no
            target_token_id = yes_token_id if target_side == "YES" else no_token_id
            
            coin = 'btc' if asset == 'BTC' else 'eth'
            
            logger.info(
                "streak_watch",
                q=q[:40],
                streak=f"{out1}-{out2}",
                streak_depth=streak_depth,
                regime="TREND_FOLLOWING" if window == 5 else "MEAN_REVERSION_FADE",
                target=target_side,
                price=target_price,
            )

            # Price floor/ceiling gates are now handled inside the decision engine (decision.py)
            # after virtual profiles evaluate, to ensure cheap signals reach profiles.

            # Signal verified and filled with dynamic side payload
            signal_payload = market.copy()
            signal_payload["target_side"] = target_side
            signal_payload["target_token_id"] = target_token_id
            signal_payload["target_price"] = target_price
            signal_payload["streak_depth"] = streak_depth
            signal_payload["streak_quality_score"] = streak_quality_score
            
            # Add quantitative streak details and telemetry
            signal_payload["t1_market_id"] = f"{coin.upper()}-updown-{duration}m-{closes_at - 2 * dur_secs}"
            signal_payload["t1_result"] = out1
            signal_payload["t2_market_id"] = f"{coin.upper()}-updown-{duration}m-{closes_at - 3 * dur_secs}"
            signal_payload["t2_result"] = out2
            signal_payload["t3_market_id"] = f"{coin.upper()}-updown-{duration}m-{closes_at - 4 * dur_secs}" if out3 != "?" else None
            signal_payload["t3_result"] = out3 if out3 != "?" else None
            signal_payload["outcome_source"] = res["source"]
            signal_payload["candle_1_delta_bps"] = res["delta1"]
            signal_payload["candle_2_delta_bps"] = res["delta2"]
            signal_payload["time_since_candle_close_secs"] = float(now - (closes_at - dur_secs))
            
            # Phase 28 Telemetry Expansion Engine Data
            strike_price = float(market.get("btc_price_at_market_open") or 0.0)
            spot_price = 0.0
            if getattr(self, "binance_ws", None) and getattr(self.binance_ws, "is_connected", False):
                spot_price = float(getattr(self.binance_ws, f"{coin}_price", 0.0))
                self._update_spot_tick(coin.upper(), spot_price, float(now))
                
            dist_bps = ((spot_price - strike_price) / strike_price * 10000.0) if strike_price > 0.0 and spot_price > 0.0 else 0.0
            spot_vel_10s = self._get_spot_velocity_10s(coin.upper())
            vol_5m = self._get_realized_volatility(coin.upper(), 300)
            vol_1h = self._get_realized_volatility(coin.upper(), 3600)
            
            target_book = self.ws.book_cache.get(target_token_id) if (target_token_id and self.ws) else None
            best_ask_val = live_yes if target_side == "YES" else live_no
            best_bid_val = (1.0 - live_no) if target_side == "YES" else (1.0 - live_yes)
            d_1c, d_5c, ask_1c, bid_1c = self._compute_orderbook_depths(target_book, best_ask_val, best_bid_val)
            
            imb_deque = self._imbalance_deques.setdefault(mid, deque(maxlen=10))
            current_imb = market.get("book_imbalance", 0.0)
            imb_deque.append(current_imb)
            imb_mom_10s = (imb_deque[-1] - imb_deque[0]) if len(imb_deque) > 1 else 0.0

            signal_payload["underlying_strike_price"] = strike_price
            signal_payload["underlying_spot_price"] = spot_price
            signal_payload["underlying_distance_bps"] = dist_bps
            signal_payload["underlying_spot_velocity_10s"] = spot_vel_10s
            signal_payload["underlying_realized_vol_5m"] = vol_5m
            signal_payload["underlying_realized_vol_1h"] = vol_1h
            signal_payload["sim_slippage_bps_at_target"] = yes_slippage if target_side == "YES" else no_slippage
            signal_payload["depth_usd_1c"] = d_1c
            signal_payload["depth_usd_5c"] = d_5c
            signal_payload["book_imbalance_momentum_10s"] = imb_mom_10s
            signal_payload["ask_depth_usd_1c"] = ask_1c
            signal_payload["bid_depth_usd_1c"] = bid_1c
            
            confirmed.append(signal_payload)

            logger.info(
                "signal_confirmed",
                q=q,
                streak=f"{out1}-{out2}",
                streak_depth=streak_depth,
                target_side=target_side,
                target_price=target_price,
                streak_quality_score=streak_quality_score
            )

        return confirmed

    async def _verify_token_mapping(self, market: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Verify YES/NO token IDs against CLOB REST API (Source of Truth).
        """
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
        Sync tracked markets stats.
        """
        self.scan_count += 1
        scanned = len(self.tracked_markets)
        flagged = sum(1 for m in self.tracked_markets.values() if m.get("consecutive_cycles_above", 0) > 0)
        return scanned, flagged

    def _simulate_fak_sweep(self, book_side: Dict[float, float], budget_usdc: float) -> Tuple[float, float, int]:
        """
        Simulates sweeping the order book side (asks) for a given budget in USDC.
        
        Returns:
            (avg_price, slippage_cents, fillable)
        """
        if not book_side:
            return 0.0, 0.0, 0
            
        sorted_levels = sorted(book_side.items(), key=lambda x: x[0])
        remaining_budget = budget_usdc
        total_shares = 0.0
        
        for price, size in sorted_levels:
            if price <= 0:
                continue
            max_cost = price * size
            if remaining_budget >= max_cost:
                total_shares += size
                remaining_budget -= max_cost
            else:
                total_shares += remaining_budget / price
                remaining_budget = 0.0
                break
                
        if remaining_budget > 0.01:
            return 0.0, 0.0, 0
            
        avg_price = budget_usdc / total_shares if total_shares > 0 else 0.0
        best_price = sorted_levels[0][0]
        slippage_cents = (avg_price - best_price) * 100
        
        return round(avg_price, 4), round(slippage_cents, 4), 1

    async def _measure_api_latency(self):
        """Measures network round-trip time to Polymarket server to simulate/mimic FAK order delay."""
        t0 = time.time()
        try:
            await self.gamma.get_true_now()
            self.last_api_latency_ms = (time.time() - t0) * 1000.0
        except:
            self.last_api_latency_ms = 0.0
