"""
Market Scanner — Streak Momentum Poll & Discovery for Polymarket Reverse Bot.
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
        elif "sol" in slug or "solana" in question or "sol" in question:
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
                            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    btc_price_open = float(data.get("price", 0.0))
                                    logger.info("fetched_btc_price_for_new_markets", btc_price=btc_price_open)
                    except Exception as e:
                        logger.error("failed_to_fetch_btc_price_for_new_markets", error=str(e))

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
                    await upsert_market(self.db, m)

                    await self.ws.subscribe_market(
                        mid, 
                        yes_token_id=m.get("yes_token_id"), 
                        no_token_id=m.get("no_token_id"),
                        condition_id=m.get("condition_id"), 
                        push_state=False
                    )
                        
                # Process Market Removal (With Grace Period)
                dead_ids = set(self.tracked_markets.keys()) - set(valid_markets.keys())

                # Reset grace counter for markets that are currently valid
                for mid in valid_markets.keys():
                    self._dead_count.pop(mid, None)

                for mid in dead_ids:
                    # Increment death counter
                    self._dead_count[mid] = self._dead_count.get(mid, 0) + 1

                    # Only unsubscribe if missing for 3+ consecutive cycles (30 seconds)
                    if self._dead_count[mid] < 3:
                        logger.debug("unsub_grace_period_active", market_id=mid, cycles=self._dead_count[mid])
                        continue

                    # SAFETY: Don't unsubscribe if we are currently holding a position!
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

    def _should_track_market(self, market: Dict[str, Any], now_override: Optional[int] = None) -> bool:
        mid = market.get("market_id")
        if not market.get("accepting_orders"): return False
        if not market.get("active", True): return False

        now = now_override if now_override is not None else int(time.time())
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
        
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_time_ms}&limit=1"
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
            logger.error("binance_candle_fetch_failed", symbol=symbol, start_time=start_time, error=str(e))
        return 0.0, 0.0, "?", 0.0

    async def _get_binance_outcome(self, coin: str, duration: int, start_time: int) -> str:
        """
        Fetch the outcome of a completed candle from the public Binance Klines API.
        Returns: 'UP', 'DOWN', or '?'
        """
        # Safety check: Is the candle fully completed?
        now = int(time.time())
        if now < start_time + duration * 60:
            logger.debug("binance_candle_not_yet_completed", coin=coin, start_time=start_time)
            return "?"

        _, _, outcome, _ = await self._get_binance_candle_data(coin, duration, start_time)
        return outcome

    async def _get_historical_outcomes(self, asset: str, duration: int, closes_at: int) -> dict:
        """
        Derive slugs for the last 6 completed markets and fetch their outcomes + deltas.
        Returns: Dict containing out1..out6, sources, deltas, and combined source.
        """
        dur_secs = duration * 60
        t1 = closes_at - (2 * dur_secs)
        t2 = closes_at - (3 * dur_secs)
        t3 = closes_at - (4 * dur_secs)
        t4 = closes_at - (5 * dur_secs)
        t5 = closes_at - (6 * dur_secs)
        t6 = closes_at - (7 * dur_secs)
        
        asset_lower = asset.lower()
        if 'bitcoin' in asset_lower or 'btc' in asset_lower:
            coin = 'btc'
        elif 'ethereum' in asset_lower or 'eth' in asset_lower:
            coin = 'eth'
        else:
            return {
                "out1": "?", "out2": "?", "out3": "?", "out4": "?", "out5": "?", "out6": "?",
                "src1": "UNKNOWN", "src2": "UNKNOWN", "src3": "UNKNOWN", "src4": "UNKNOWN", "src5": "UNKNOWN", "src6": "UNKNOWN",
                "delta1": 0.0, "delta2": 0.0, "delta3": 0.0, "delta4": 0.0, "delta5": 0.0, "delta6": 0.0,
                "source": "UNKNOWN"
            }
            
        slug1 = f"{coin}-updown-{duration}m-{t1}"
        slug2 = f"{coin}-updown-{duration}m-{t2}"
        slug3 = f"{coin}-updown-{duration}m-{t3}"
        slug4 = f"{coin}-updown-{duration}m-{t4}"
        slug5 = f"{coin}-updown-{duration}m-{t5}"
        slug6 = f"{coin}-updown-{duration}m-{t6}"
        
        results = []
        for slug, t_start in ((slug1, t1), (slug2, t2), (slug3, t3), (slug4, t4), (slug5, t5), (slug6, t6)):
            if slug in self._outcome_cache and isinstance(self._outcome_cache[slug], dict):
                results.append(self._outcome_cache[slug])
                continue
                
            logger.info("fetching_historical_outcome", slug=slug)
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
                                    if len(self._outcome_cache) >= 200 and slug not in self._outcome_cache:
                                        try:
                                            oldest_key = next(iter(self._outcome_cache))
                                            del self._outcome_cache[oldest_key]
                                            logger.debug("outcome_cache_pruned", evicted_key=oldest_key)
                                        except Exception as cache_err:
                                            logger.warning("failed_to_prune_outcome_cache", error=str(cache_err))
                                    self._outcome_cache[slug] = val
                                    results.append(val)
                                    continue
                
                # 2. Fall back to Binance if Gamma unresolved
                logger.info("gamma_outcome_missing_falling_back_to_binance", slug=slug, start_time=t_start)
                _, _, b_outcome, b_delta = await self._get_binance_candle_data(coin, duration, t_start)
                if b_outcome != "?":
                    outcome = b_outcome
                    source = "BINANCE_FALLBACK"
                    delta_bps = b_delta
                    
                    val = {"outcome": outcome, "source": source, "delta_bps": delta_bps}
                    if len(self._outcome_cache) >= 200 and slug not in self._outcome_cache:
                        try:
                            oldest_key = next(iter(self._outcome_cache))
                            del self._outcome_cache[oldest_key]
                            logger.debug("outcome_cache_pruned", evicted_key=oldest_key)
                        except Exception as cache_err:
                            logger.warning("failed_to_prune_outcome_cache", error=str(cache_err))
                    self._outcome_cache[slug] = val
                    results.append(val)
                else:
                    results.append({"outcome": "?", "source": "UNKNOWN", "delta_bps": 0.0})
            except Exception as e:
                logger.warning("failed_fetching_historical_outcome", slug=slug, error=str(e))
                results.append({"outcome": "?", "source": "ERROR", "delta_bps": 0.0})
                
        res1 = results[0]
        res2 = results[1]
        res3 = results[2]
        res4 = results[3]
        res5 = results[4]
        res6 = results[5]
        
        combined_source = "GAMMA"
        if any(r["source"] == "BINANCE_FALLBACK" for r in results):
            combined_source = "BINANCE_FALLBACK"
        elif any(r["source"] == "UNKNOWN" for r in results):
            combined_source = "UNKNOWN"
            
        return {
            "out1": res1["outcome"],
            "out2": res2["outcome"],
            "out3": res3["outcome"],
            "out4": res4["outcome"],
            "out5": res5["outcome"],
            "out6": res6["outcome"],
            "src1": res1["source"],
            "src2": res2["source"],
            "src3": res3["source"],
            "src4": res4["source"],
            "src5": res5["source"],
            "src6": res6["source"],
            "delta1": res1["delta_bps"],
            "delta2": res2["delta_bps"],
            "delta3": res3["delta_bps"],
            "delta4": res4["delta_bps"],
            "delta5": res5["delta_bps"],
            "delta6": res6["delta_bps"],
            "source": combined_source
        }

    async def get_entry_signals(self) -> List[Dict[str, Any]]:
        """
        Check for Streak Momentum entry signals across all tracked markets using WebSocket cache.
        Checks if the last 2 completed micro-markets resolved to the same side (UP-UP or DOWN-DOWN).
        """
        confirmed = []
        now = int(time.time())

        for mid, market in list(self.tracked_markets.items()):
            closes_at = market.get("closes_at", 0)
            if closes_at > 0 and now > closes_at:
                await self.ws.unsubscribe_market(
                    mid,
                    yes_token_id=market.get("yes_token_id"),
                    no_token_id=market.get("no_token_id"),
                    push_state=True
                )
                del self.tracked_markets[mid]
                continue
            duration = market.get("window_minutes")
            if duration not in (5, 15):
                continue

            symbol, window = self._get_market_key_info(market)
            
            # --- TICK-BY-TICK SPREAD LOGGING FOR QUANT ANALYSIS ---
            # Retrieve real-time L2 best bid and ask from the WebSocket client
            yes_tid = market.get("yes_token_id")
            no_tid = market.get("no_token_id")
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
                    
                    # Calculate FOK sweep in-memory using WS book_cache
                    budget = float(self.config.position_size_usdc)
                    
                    # 1. YES sweep
                    yes_book = self.ws.book_cache.get(yes_tid) if yes_tid else None
                    yes_asks = yes_book.get("asks", {}) if yes_book else {}
                    yes_fok_price, yes_slippage, yes_fillable = self._simulate_fok_sweep(yes_asks, budget)
                    
                    # 2. NO sweep
                    no_book = self.ws.book_cache.get(no_tid) if no_tid else None
                    no_asks = no_book.get("asks", {}) if no_book else {}
                    no_fok_price, no_slippage, no_fillable = self._simulate_fok_sweep(no_asks, budget)
                    
                    # 3. Latency measurement (throttled background task)
                    now_time = time.time()
                    if now_time - self._last_latency_check >= 5.0:
                        self._last_latency_check = now_time
                        asyncio.create_task(self._measure_api_latency())
                    
                    # Store tick in the high-performance spread_logs table
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
                        sim_fok_yes_price=float(yes_fok_price),
                        sim_fok_yes_slippage=float(yes_slippage),
                        sim_fok_yes_fillable=int(yes_fillable),
                        sim_fok_no_price=float(no_fok_price),
                        sim_fok_no_slippage=float(no_slippage),
                        sim_fok_no_fillable=int(no_fillable),
                        api_latency_ms=float(self.last_api_latency_ms)
                    ))

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
            out4 = res["out4"]
            out5 = res["out5"]
            out6 = res["out6"]
            
            if out1 == "?" or out2 == "?":
                # Outcomes not yet resolved (waiting for Chainlink settlement)
                logger.debug("historical_outcomes_lagging", market_id=mid, slug=market.get("slug"), out1=out1, out2=out2)
                continue
                
            # 2. Streak Momentum Match
            if out1 != out2:
                # Oscillating/choppy market — stay out
                continue

            # Determine dynamic streak depth
            streak_depth = 2
            if out3 != "?" and out1 == out2 == out3:
                streak_depth = 3
                if out4 != "?" and out1 == out2 == out3 == out4:
                    streak_depth = 4
                    if out5 != "?" and out1 == out2 == out3 == out4 == out5:
                        streak_depth = 5
                        if out6 != "?" and out1 == out2 == out3 == out4 == out5 == out6:
                            streak_depth = 6

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

            # Determine dynamic regime side based on timeframe window size and streak depth:
            # 5m window:
            #   - Streak depth 2: Trend-Following (buy SAME direction as the streak)
            #   - Streak depth 3 or more: Mean Reversion / Reversal (buy OPPOSITE direction to the streak)
            # 15m window:
            #   - Always Mean Reversion / Fade (buy OPPOSITE direction to the streak)
            if window == 5:
                if streak_depth >= 3:
                    # Scale-in reversal / fade for 5m
                    target_side = "NO" if out1 == "UP" else "YES"
                else:
                    # Standard trend-following for 5m
                    target_side = "YES" if out1 == "UP" else "NO"
            else:
                # Always fade for 15m
                target_side = "NO" if out1 == "UP" else "YES"
            
            yes_token_id = market.get("yes_token_id")
            no_token_id  = market.get("no_token_id")
            
            live_yes = self.ws.ask_cache.get(yes_token_id) or self.ws.price_cache.get(yes_token_id)
            live_no  = self.ws.ask_cache.get(no_token_id)  or self.ws.price_cache.get(no_token_id)

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

            # 3. Apply Entry Price Band Guard (Floor + Ceiling)
            # Read from config: defaults to $0.45 floor / $0.65 ceiling
            min_entry_price = getattr(self.config, "min_entry_price", 0.45)
            max_entry_price = getattr(self.config, "max_entry_price", 0.65)
            
            is_reversal = (streak_depth >= 3) if duration == 5 else (streak_depth >= 2)
            
            if not is_reversal and target_price < min_entry_price:
                logger.info(
                    "quality_filter_price_floor",
                    market_id=mid,
                    target_side=target_side,
                    target_price=target_price,
                    min_entry=min_entry_price,
                    message=f"Target price {target_price} below minimum floor {min_entry_price} — Deep Value Trap filter"
                )
                continue

            if not is_reversal and target_price > max_entry_price:
                logger.info(
                    "quality_filter_price_ceiling",
                    market_id=mid,
                    target_side=target_side,
                    target_price=target_price,
                    max_entry=max_entry_price,
                    message=f"Target price {target_price} exceeds maximum ceiling {max_entry_price}"
                )
                continue

            # Signal verified and filled with dynamic side payload
            signal_payload = market.copy()
            signal_payload["target_side"] = target_side
            signal_payload["target_token_id"] = target_token_id
            signal_payload["target_price"] = target_price
            signal_payload["streak_depth"] = streak_depth
            
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
            
            confirmed.append(signal_payload)
            logger.info(
                "signal_confirmed",
                q=q,
                streak=f"{out1}-{out2}",
                streak_depth=streak_depth,
                target_side=target_side,
                target_price=target_price
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

    def _simulate_fok_sweep(self, book_side: Dict[float, float], budget_usdc: float) -> Tuple[float, float, int]:
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
        """Measures network round-trip time to Polymarket server to simulate/mimic FOK order delay."""
        t0 = time.time()
        try:
            await self.gamma.get_true_now()
            self.last_api_latency_ms = (time.time() - t0) * 1000.0
        except:
            self.last_api_latency_ms = 0.0
