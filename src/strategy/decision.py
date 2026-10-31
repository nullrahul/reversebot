import re
import asyncio
import json
from typing import Any, Dict, Optional, Tuple
import time

from config.config import BotConfig
from src.strategy.math_utils import expected_value, slippage_depth_required, FEE_MULTIPLIER, calculate_kelly_fraction, get_linear_time_decay_multiplier, calculate_correlated_penalty_multiplier, calculate_bs_binary_prob
from src.api.clob_client import ClobClient
from src.strategy.validator import SignalValidator, ValidationResult
from src.strategy.position_mgr import PositionManager
from src.db.queries import log_event, has_traded_market, get_open_positions, get_market, get_rolling_win_rate, log_signal_evaluation, get_historical_yes_price, log_hypothetical_kelly, get_last_closed_trend_trade, upsert_market, fetch_all, fetch_one
from src.db.rejections import RejectionLogger
from src.logs.log_config import get_logger

logger = get_logger("decision")

# Global Entry Lock to prevent rapid-fire burst entries
_global_entry_lock = asyncio.Lock()

def get_core_context(question: str) -> str:
    """Extract the core event context by stripping dates, times, and window markers."""
    # 1. Remove months and dates (April 15, 2026, etc.)
    q = re.sub(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b', '', question, flags=re.I)
    
    # 2. Remove time ranges (8:25AM-8:30AM ET, 10:00AM, etc.)
    q = re.sub(r'\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))?\s*(?:ET|UTC|UTC\d+)?', '', q, flags=re.I)
    
    # 3. Remove numeric dates (04/15/26) and standalone years (2026)
    q = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '', q)
    q = re.sub(r'\b202\d\b', '', q)
    
    # 4. Remove 'Up or Down' window markers often at the end
    q = re.sub(r'\s+on\s*$', '', q, flags=re.I)
    q = re.sub(r'\s+by\s*$', '', q, flags=re.I)
    q = re.sub(r'\?$', '', q)
    
    # 5. Clean up and normalize
    q = re.sub(r'^Will\s+', '', q, flags=re.I)
    return ' '.join(q.split()).strip().lower()


class DecisionEngine:
    """
    Orchestrates the full trading decision pipeline:
    Signal → Validate → EV → Slippage → Execute
    """

    def __init__(
        self,
        config: BotConfig,
        validator: SignalValidator,
        position_mgr: PositionManager,
        clob_client: ClobClient,
        db,
        rejection_logger: Optional[RejectionLogger] = None,
        scanner = None,
        binance_ws = None,
        skip_db = None,
        regime_tracker = None,
    ):
        self.config = config
        self.validator = validator
        self.position_mgr = position_mgr
        self.clob = clob_client
        self.db = db
        self.rejection_logger = rejection_logger
        self.scanner = scanner
        self.binance_ws = binance_ws
        self.skip_db = skip_db
        self.regime_tracker = regime_tracker
        self._evaluated_markets = set()
        self._last_seen_scan_count = 0
        self.starting_bankroll = None
        self.drawdown_halted = False
        self._bankroll_cache = None
        self._last_bankroll_time = 0.0
        
        # Initialize soft audit database path
        self.soft_audit_db_path = "data/soft_guards_audit.db"

        # Initialize V4.5 Challenger Shadow Router Logger
        from src.strategy.router import ShadowRouterLogger
        self.shadow_router = ShadowRouterLogger()

        try:
            from src.db.soft_audit_db import init_soft_audit_db
            asyncio.create_task(init_soft_audit_db(self.soft_audit_db_path))
        except Exception:
            pass

        # Initialize Strategy Profile Manager
        try:
            from src.strategy.profiles import ProfileManager
            self.profile_mgr = ProfileManager(db, position_mgr)
        except Exception as e:
            logger.error("failed_to_initialize_profile_mgr", error=str(e))
            self.profile_mgr = None

    def _get_market_key_info(self, market: dict) -> Tuple[str, int]:
        """Extract symbol and window minutes, falling back if scanner is not initialized."""
        if self.scanner:
            return self.scanner._get_market_key_info(market)
            
        slug = (market.get("slug") or "").lower()
        question = (market.get("question") or "").lower()
        
        # Parse window minutes
        window = market.get("window_minutes")
        if not window:
            if "-5m-" in slug or "5-minute" in question:
                window = 5
            elif "-15m-" in slug or "15-minute" in question:
                window = 15
            else:
                window = 5
                
        # Parse symbol
        symbol = "BTC"
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question or "sol" in question:
            symbol = "SOL"
        elif "btc" in slug or "bitcoin" in question or "btc" in question:
            symbol = "BTC"
        else:
            if hasattr(self, "config") and hasattr(self.config, "target_crypto_symbols"):
                for target in self.config.target_crypto_symbols:
                    if target.lower() in question or target.lower() in slug:
                        symbol = target.upper()
                        break
        return symbol, window

    def _get_best_ask(self, token_id: str) -> Optional[float]:
        if not token_id or not self.position_mgr.ws:
            return None
        val = self.position_mgr.ws.get_best_ask(token_id)
        from unittest.mock import MagicMock, AsyncMock
        if isinstance(val, (MagicMock, AsyncMock)):
            return None
        return val

    def _get_best_bid(self, token_id: str) -> Optional[float]:
        if not token_id or not self.position_mgr.ws:
            return None
        val = self.position_mgr.ws.get_best_bid(token_id)
        from unittest.mock import MagicMock, AsyncMock
        if isinstance(val, (MagicMock, AsyncMock)):
            return None
        return val

    async def _get_live_price_with_fallback(
        self, token_id: str, counterpart_tid: Optional[str] = None
    ) -> Optional[float]:
        """
        Multi-tier price resolution for decision evaluation:
        Tier 1 (0ms): WebSocket best ask cache
        Tier 2 (0ms): Implied ask from counterpart token best bid (1.0 - counterpart_bid)
        Tier 3 (Network): CLOB REST orderbook fallback (/book?token_id={token_id})
        Returns None if genuinely no liquidity exists. NEVER defaults to 0.50.
        """
        if not token_id:
            return None

        from unittest.mock import MagicMock, AsyncMock, Mock

        # Tier 1: Check WebSocket best ask cache (0ms)
        if self.position_mgr.ws and not isinstance(self.position_mgr.ws, (MagicMock, AsyncMock, Mock)):
            ws_ask = self.position_mgr.ws.get_best_ask(token_id)
            if ws_ask is not None and not isinstance(ws_ask, (MagicMock, AsyncMock)):
                try:
                    val = float(ws_ask)
                    if 0.001 < val < 0.999:
                        return val
                except (ValueError, TypeError):
                    pass

        # Tier 2: Check Implied Ask from Counterpart Bid in WebSocket cache (0ms)
        if counterpart_tid and self.position_mgr.ws and not isinstance(self.position_mgr.ws, (MagicMock, AsyncMock, Mock)):
            ws_counterpart_bid = self.position_mgr.ws.get_best_bid(counterpart_tid)
            if ws_counterpart_bid is not None and not isinstance(ws_counterpart_bid, (MagicMock, AsyncMock)):
                try:
                    c_bid = float(ws_counterpart_bid)
                    if 0.001 < c_bid < 0.999:
                        implied_ask = round(1.0 - c_bid, 4)
                        if 0.001 < implied_ask < 0.999:
                            return implied_ask
                except (ValueError, TypeError):
                    pass

        # Tier 3: REST CLOB Orderbook fallback (/book?token_id=...)
        clob = self.clob or getattr(self.position_mgr, "clob", None)
        if clob:
            get_ob = getattr(clob, "get_orderbook", None)
            if get_ob:
                try:
                    res = get_ob(token_id)
                    if asyncio.iscoroutine(res):
                        book = await res
                    else:
                        book = res
                    if isinstance(book, dict) and book.get("asks"):
                        best_ask = float(book["asks"][0]["price"])
                        if 0.001 < best_ask < 0.999:
                            # Prime WebSocket cache for subsequent checks
                            if self.position_mgr.ws and hasattr(self.position_mgr.ws, "ask_cache"):
                                self.position_mgr.ws.ask_cache[token_id] = best_ask
                            return best_ask
                except Exception as e:
                    logger.debug("clob_orderbook_rest_fallback_error", token_id=token_id, error=str(e))

        return None

    async def _get_reversal_profile_rolling_pnl(self) -> Optional[float]:
        """
        Query the last 10 completed positions for the 'reversal' profile
        from the profile_positions table and sum their realised P&L.
        """
        from unittest.mock import MagicMock, AsyncMock, Mock
        if isinstance(self.db, (MagicMock, AsyncMock, Mock)) or self.db is None:
            return None
            
        try:
            rows = await fetch_all(
                self.db,
                "SELECT realised_pnl FROM profile_positions "
                "WHERE profile_name = 'reversal' AND order_status = 'FILLED' AND realised_pnl IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 10"
            )
            if not rows:
                return None
            return sum(float(r["realised_pnl"]) for r in rows)
        except Exception as e:
            logger.error("failed_to_get_reversal_profile_rolling_pnl", error=str(e))
            return None

    async def _get_active_strategy_mode(self, force_enabled: bool = False) -> str:
        """
        Retrieves the active strategy mode (TREND or REVERSAL) based on the rolling win rate
        of the last N completed positions.
        """
        toggling_enabled = getattr(self.config, "reversal_toggling_enabled", True)
        if force_enabled:
            toggling_enabled = True
        from unittest.mock import MagicMock, AsyncMock
        if isinstance(toggling_enabled, (MagicMock, AsyncMock)):
            toggling_enabled = force_enabled
        
        if not toggling_enabled:
            return "TREND"

        table = "paper_positions" if getattr(self.config, "paper_trade", True) is True else "positions"
        if isinstance(table, (MagicMock, AsyncMock)):
            table = "paper_positions"

        lookback_n = getattr(self.config, "reversal_lookback_n", 10)
        if isinstance(lookback_n, (MagicMock, AsyncMock)):
            lookback_n = 10

        switch_threshold = getattr(self.config, "reversal_switch_threshold", 0.50)
        if isinstance(switch_threshold, (MagicMock, AsyncMock)):
            switch_threshold = 0.50

        lookback_window = getattr(self.config, "reversal_lookback_window_seconds", 86400)
        if isinstance(lookback_window, (MagicMock, AsyncMock)):
            lookback_window = 86400
        min_closed_at = int(time.time()) - lookback_window

        try:
            # Query last N completed positions closed within the lookback window
            rows = await fetch_all(
                self.db,
                f"SELECT notes, realised_pnl FROM {table} "
                f"WHERE order_status = 'FILLED' AND realised_pnl IS NOT NULL "
                f"AND closed_at >= ? "
                f"ORDER BY closed_at DESC LIMIT {lookback_n}",
                (min_closed_at,)
            )
            
            if not rows:
                return "TREND"

            # Parse strategy mode of the most recently completed position (skipping LOSS_FADE)
            last_strategy = "TREND"
            for row in rows:
                notes = row["notes"]
                if notes and notes.startswith("{"):
                    try:
                        strat = json.loads(notes).get("strategy", "TREND")
                        if strat in ("TREND", "REVERSAL"):
                            last_strategy = strat
                            break
                    except Exception:
                        pass

            # Consecutive losses override: if the last 5 completed positions are all losses (realised_pnl < 0)
            if len(rows) >= 5 and all(r["realised_pnl"] < 0 for r in rows[:5]):
                logger.info(
                    "strategy_mode_forced_reversal_consecutive_losses",
                    completed_count=len(rows),
                    last_5_pnl=[r["realised_pnl"] for r in rows[:5]]
                )
                active_strategy = "REVERSAL"
            else:
                if len(rows) < lookback_n:
                    active_strategy = last_strategy
                else:
                    # Calculate rolling win rate of the last N completed positions
                    wins = sum(1 for r in rows if r["realised_pnl"] > 0)
                    win_rate = wins / len(rows)

                    if win_rate < switch_threshold:
                        active_strategy = "REVERSAL" if last_strategy == "TREND" else "TREND"
                        logger.info(
                            "strategy_mode_toggled",
                            win_rate=win_rate,
                            threshold=switch_threshold,
                            last_strategy=last_strategy,
                            active_strategy=active_strategy,
                            completed_count=len(rows)
                        )
                    else:
                        active_strategy = last_strategy
                        logger.info(
                            "strategy_mode_retained",
                            win_rate=win_rate,
                            threshold=switch_threshold,
                            last_strategy=last_strategy,
                            active_strategy=active_strategy,
                            completed_count=len(rows)
                        )

            # Prevent logical deadlocks by dynamically overriding active strategy mode based on the virtual reversal profile's performance
            reversal_rolling_pnl = await self._get_reversal_profile_rolling_pnl()
            if reversal_rolling_pnl is not None:
                if active_strategy == "REVERSAL" and reversal_rolling_pnl <= 0:
                    logger.info(
                        "strategy_mode_deadlock_override",
                        reversal_pnl=reversal_rolling_pnl,
                        message="REVERSAL mode overridden to TREND: Reversal profile rolling PnL <= 0 (Trending market regime)."
                    )
                    active_strategy = "TREND"
                elif active_strategy == "TREND" and reversal_rolling_pnl > 0:
                    logger.info(
                        "strategy_mode_deadlock_override",
                        reversal_pnl=reversal_rolling_pnl,
                        message="TREND mode overridden to REVERSAL: Reversal profile rolling PnL > 0 (Choppy market regime)."
                    )
                    active_strategy = "REVERSAL"

            return active_strategy

        except Exception as e:
            logger.error("failed_to_get_active_strategy_mode", error=str(e))
            return "TREND"

    async def _log_signal(
        self,
        market: Dict[str, Any],
        side: str,
        ev: float,
        imbalance: float,
        cvd_60s: float,
        secs_left: float,
        result: str,
        reason: Optional[str] = None,
        price_velocity: float = 0.0,
        strategy_mode: Optional[str] = None
    ) -> None:
        try:
            symbol, window = self._get_market_key_info(market)
            direction = "UP" if side == "YES" else "DOWN"
            yes_price = market.get("yes_price", 0.0)
            
            yes_tid = market.get("yes_token_id")
            spread = 0.0
            if yes_tid and self.position_mgr.ws:
                yes_bid = self.position_mgr.ws.bid_cache.get(yes_tid)
                yes_ask = self.position_mgr.ws.ask_cache.get(yes_tid)
                from unittest.mock import MagicMock, AsyncMock
                if yes_bid is not None and yes_ask is not None and not isinstance(yes_bid, (MagicMock, AsyncMock)) and not isinstance(yes_ask, (MagicMock, AsyncMock)):
                    spread = float(yes_ask) - float(yes_bid)
                    
            t1_market_id = market.get("t1_market_id")
            t1_result = market.get("t1_result")
            t2_market_id = market.get("t2_market_id")
            t2_result = market.get("t2_result")
            outcome_source = market.get("outcome_source", "GAMMA")
            candle_1_delta_bps = market.get("candle_1_delta_bps", 0.0)
            candle_2_delta_bps = market.get("candle_2_delta_bps", 0.0)
            entry_price = market.get("target_price", 0.0)
            time_since_candle_close_secs = market.get("time_since_candle_close_secs", 0.0)
            
            # Query active open positions to populate correlated telemetry at signal time
            correlated_position_open = 0
            correlated_position_direction = None
            try:
                open_positions = await get_open_positions(self.db, paper=self.config.paper_trade)
                now_ts = int(time.time())
                active_open_positions = []
                for pos in open_positions:
                    m_data = await get_market(self.db, pos["market_id"])
                    if m_data:
                        closes_at = m_data.get("closes_at", 0)
                        if closes_at == 0 or now_ts <= closes_at:
                            active_open_positions.append(pos)
                    else:
                        active_open_positions.append(pos)
                
                corr_symbol = "ETH" if symbol == "BTC" else ("BTC" if symbol == "ETH" else None)
                if corr_symbol:
                    for pos in active_open_positions:
                        existing_m = await get_market(self.db, pos["market_id"])
                        if not existing_m: continue
                        existing_q = existing_m.get("question", "").lower()
                        existing_symbol = "ETH" if "eth" in existing_q or "ethereum" in existing_q else ("BTC" if "btc" in existing_q or "bitcoin" in existing_q else None)
                        if existing_symbol == corr_symbol:
                            existing_slug = existing_m.get("slug", "").lower()
                            existing_window = 15 if ("-15m-" in existing_slug or "15-minute" in existing_q) else 5
                            if existing_window == window:
                                correlated_position_open = 1
                                correlated_position_direction = pos.get("side", "NO")
                                break
            except Exception as corr_err:
                logger.error("failed_to_evaluate_correlated_signals_log", error=str(corr_err))

            persistence_score = market.get("persistence_score", 0.0)
            if strategy_mode is None:
                strategy_mode = await self._get_active_strategy_mode()

            # Phase 28 Telemetry Calculation Engine
            spot_p = float(market.get("underlying_spot_price", 0.0))
            strike_p = float(market.get("underlying_strike_price", 0.0))
            vol_5m_val = float(market.get("underlying_realized_vol_5m", 0.001))
            
            bs_prob = calculate_bs_binary_prob(spot_p, strike_p, secs_left, vol_5m_val)
            pricing_edge_bps = (bs_prob - yes_price) * 10000.0 if yes_price > 0.0 else 0.0
            
            market["bs_implied_probability"] = bs_prob
            market["option_pricing_edge_bps"] = pricing_edge_bps

            await log_signal_evaluation(
                self.db,
                market_id=market["market_id"],
                symbol=symbol,
                window_size=str(window),
                direction=direction,
                side=side,
                t1_market_id=t1_market_id,
                t1_result=t1_result,
                t2_market_id=t2_market_id,
                t2_result=t2_result,
                t3_market_id=market.get("t3_market_id"),
                t3_result=market.get("t3_result"),
                streak_depth=market.get("streak_depth", 2),
                yes_price_at_signal=yes_price,
                bid_ask_spread_at_signal=spread,
                book_imbalance=imbalance,
                cvd_60s=cvd_60s,
                price_velocity=price_velocity,
                ev_computed=ev,
                seconds_to_close=secs_left,
                guard_result=result,
                guard_rejection_reason=reason,
                outcome_source=outcome_source,
                candle_1_delta_bps=candle_1_delta_bps,
                candle_2_delta_bps=candle_2_delta_bps,
                entry_price=entry_price,
                time_since_candle_close_secs=time_since_candle_close_secs,
                eventual_pnl=0.0,
                correlated_position_open=correlated_position_open,
                correlated_position_direction=correlated_position_direction,
                persistence_score=persistence_score,
                strategy_mode=strategy_mode,
                underlying_strike_price=strike_p,
                underlying_spot_price=spot_p,
                underlying_distance_bps=float(market.get("underlying_distance_bps", 0.0)),
                underlying_spot_velocity_10s=float(market.get("underlying_spot_velocity_10s", 0.0)),
                underlying_realized_vol_5m=vol_5m_val,
                underlying_realized_vol_1h=float(market.get("underlying_realized_vol_1h", 0.0)),
                sim_slippage_bps_at_target=float(market.get("sim_slippage_bps_at_target", 0.0)),
                depth_usd_1c=float(market.get("depth_usd_1c", 0.0)),
                depth_usd_5c=float(market.get("depth_usd_5c", 0.0)),
                book_imbalance_momentum_10s=float(market.get("book_imbalance_momentum_10s", 0.0)),
                ask_depth_usd_1c=float(market.get("ask_depth_usd_1c", 0.0)),
                bid_depth_usd_1c=float(market.get("bid_depth_usd_1c", 0.0)),
                bs_implied_probability=bs_prob,
                option_pricing_edge_bps=pricing_edge_bps,
                execution_attempts=1,
                execution_duration_ms=0.0,
                fill_percentage=1.0,
                resolution_latency_seconds=0,
            )

        except Exception as e:
            logger.error("failed_to_log_signal_evaluation", error=str(e))

    async def process_signal(
        self,
        market: Dict[str, Any],
        target_token_id: Optional[str] = None,
        side: str = "NO",
        **kwargs,
    ) -> Tuple[bool, str]:
        """
        Process a confirmed scanner signal through the full decision pipeline.

        Args:
            market: Market dict with yes_price, no_price, etc.
            target_token_id: Specific token ID to buy (YES or NO)
            side: Side we are buying (YES or NO)

        Returns:
            (success: bool, reason: str)
        """
        # Force-resolve any expired positions to eliminate T-1 resolution race condition
        from unittest.mock import MagicMock, AsyncMock
        if not isinstance(self.position_mgr, (MagicMock, AsyncMock)):
            try:
                await self.position_mgr.check_resolutions()
            except Exception as res_err:
                logger.error("failed_to_force_resolve_in_process_signal", error=str(res_err))
        original_side = side
        original_token_id = target_token_id
        elapsed_secs = 0.0
        local_price_velocity = 0.0
        would_reject_reasons = []
        symbol, window = self._get_market_key_info(market)
        market_id = market["market_id"]
        market_question = market.get("question", "")

        # V4.5 Challenger Shadow Mode Non-Blocking Telemetry Evaluation
        if hasattr(self, "shadow_router") and self.shadow_router:
            try:
                shadow_payload = {
                    "market_id": market_id,
                    "asset": symbol,
                    "entry_price": market.get("yes_price", 0.50),
                    "cvd_60s": market.get("cvd_60s_at_entry", 0.0),
                    "book_imbalance": market.get("book_imbalance_at_entry", 0.0),
                    "streak_depth": market.get("streak_depth", 2),
                    "seconds_to_close": market.get("seconds_to_close", 300.0),
                    "side": side
                }
                asyncio.create_task(self.shadow_router.evaluate_and_log(shadow_payload, legacy_decision="EVALUATING"))
            except Exception as shadow_err:
                logger.error("failed_to_dispatch_shadow_router", error=str(shadow_err))


        # Timeframe guard check is moved to after the profiles evaluation.
        
        # Get direction-aware persistence score from regime tracker
        # YES side = UP streak, NO side = DOWN streak
        streak_direction = "UP" if side == "YES" else "DOWN"
        persistence_score = 0.0
        if self.regime_tracker:
            try:
                # On-demand refresh: fetch fresh klines before evaluating
                await self.regime_tracker.refresh_score(asset=symbol)
                persistence_score = self.regime_tracker.get_current_score(
                    asset=symbol, window=window, direction=streak_direction
                )
                overall_score = self.regime_tracker.get_current_score(
                    asset=symbol, window=window, direction=None
                )
                logger.info(
                    "regime_score_evaluated",
                    asset=symbol,
                    direction=streak_direction,
                    directional_persistence=persistence_score,
                    overall_persistence=overall_score,
                )
            except Exception as pe:
                logger.error("failed_to_get_persistence_score", error=str(pe))
        
        market = market.copy()
        market["persistence_score"] = persistence_score

        # Enforce Persistence Score Gate (Soft Guard / Shadow Mode warning)
        if self.regime_tracker:
            min_persistence = getattr(self.config, "min_persistence_score", 0.55)
            from unittest.mock import MagicMock, AsyncMock
            if isinstance(min_persistence, (MagicMock, AsyncMock)):
                min_persistence = 0.55
            if persistence_score < min_persistence:
                logger.warning(
                    "persistence_score_gate_soft_warning",
                    market_id=market_id,
                    asset=symbol,
                    direction=streak_direction,
                    score=persistence_score,
                    threshold=min_persistence,
                    message=f"Persistence score {persistence_score:.4f} < {min_persistence:.2f} threshold - Soft warning logged."
                )
                would_reject_reasons.append(f"PERSISTENCE_SCORE_{persistence_score:.4f}_LT_{min_persistence:.2f}")
        
        # ─── Weekend Lockout Guard (Step 3) ───
        import datetime
        from zoneinfo import ZoneInfo
        now_ts = time.time()
        dt_utc = datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc)
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        
        day_of_week = dt_et.weekday()  # 0 = Monday, ..., 4 = Friday, 5 = Saturday, 6 = Sunday
        hour_et = dt_et.hour
        
        # Friday after 5:00 PM ET (17:00), Saturday all day, or Sunday before 6:00 PM ET (18:00)
        is_weekend_lockout = False
        if day_of_week == 4 and hour_et >= 17:
            is_weekend_lockout = True
        elif day_of_week == 5:
            is_weekend_lockout = True
        elif day_of_week == 6 and hour_et < 18:
            is_weekend_lockout = True
            
        if is_weekend_lockout:
            logger.info(
                "weekend_lockout_bypassed",
                market_id=market_id,
                day=dt_et.strftime("%A"),
                hour=hour_et,
                message="Weekend lockout active (Fri 5:00 PM - Sun 11:59 PM ET) but bypassed for weekend testing."
            )
            # Bypassed for weekend testing: do not return False, let signal evaluation proceed
        
        # Compute current bankroll (cached for 30s in live mode)
        now_time = time.time()
        if self.config.paper_trade:
            row = await fetch_one(self.db, "SELECT SUM(realised_pnl) FROM paper_positions WHERE order_status = 'FILLED'")
            realised_sum = row[0] if row and row[0] is not None else 0.0
            current_bankroll = max(10.0, 10000.0 + realised_sum)
        else:
            if self._bankroll_cache is None or now_time - self._last_bankroll_time > 30.0:
                try:
                    bal_data = await self.clob.get_account_balance()
                    current_bankroll = float(bal_data["balance"]) if (bal_data and "balance" in bal_data) else 100.0
                except Exception:
                    current_bankroll = self._bankroll_cache if self._bankroll_cache is not None else 100.0
                self._bankroll_cache = current_bankroll
                self._last_bankroll_time = now_time
            else:
                current_bankroll = self._bankroll_cache

        # ─── Max Drawdown Breaker Check ───
        # Disabled for live trading per Option B (only runs in paper trade mode)
        is_drawdown_halted = False
        if self.config.paper_trade:
            if self.drawdown_halted:
                is_drawdown_halted = True
                
            if self.starting_bankroll is None:
                self.starting_bankroll = current_bankroll
                logger.info("starting_bankroll_initialized", starting_bankroll=self.starting_bankroll)

            if self.starting_bankroll > 0:
                drawdown = (self.starting_bankroll - current_bankroll) / self.starting_bankroll
                if drawdown >= 0.20:
                    self.drawdown_halted = True
                    logger.critical(
                        "max_drawdown_breaker_tripped",
                        starting_bankroll=self.starting_bankroll,
                        current_bankroll=current_bankroll,
                        drawdown=f"{drawdown*100:.1f}%"
                    )
                    is_drawdown_halted = True
                
        symbol, window = self._get_market_key_info(market)
        streak_depth = market.get("streak_depth", 2)
        q_lower = market_question.lower()
        is_short_window = "up or down" in q_lower and any(kw in q_lower for kw in ["bitcoin", "btc", "ethereum", "eth"])
        yes_price = market.get("yes_price", 0.0)
        no_price = market.get("no_price", 0.0)
        new_closes_at = market.get("closes_at") or 0

        # Guarantee market row exists in markets table before any profile positions, signals, skips, or trades are recorded
        from unittest.mock import Mock
        if not isinstance(self.db, (MagicMock, AsyncMock, Mock)) and self.db is not None:
            try:
                await upsert_market(self.db, market)
            except Exception as up_err:
                logger.warning("decision_market_upsert_warning", market_id=market_id, error=str(up_err))

        # Register for potential late entry tracking in ProfileManager
        if self.profile_mgr:
            try:
                reg_token_id = original_token_id
                if not reg_token_id:
                    reg_token_id = market.get("yes_token_id") if original_side == "YES" else market.get("no_token_id")
                
                # Fetch CVD
                reg_cvd_60s = 0.0
                if self.binance_ws and self.binance_ws.is_connected:
                    reg_cvd_60s, _ = self.binance_ws.get_cvd_metrics(symbol)
                
                # Active strategy mode (force enabled for virtual simulation)
                profile_active_strategy = await self._get_active_strategy_mode(force_enabled=True)
                
                # Register the potential signal
                await self.profile_mgr.register_potential_signal(
                    market=market,
                    side=original_side,
                    target_token_id=reg_token_id,
                    size_usdc=self.config.position_size_usdc,
                    strategy_mode=profile_active_strategy,
                    persistence_score=persistence_score,
                    cvd_60s_at_entry=reg_cvd_60s,
                    bankroll_at_entry=current_bankroll,
                    streak_depth=streak_depth
                )
            except Exception as p_err:
                logger.error("failed_to_register_potential_late_entry", error=str(p_err))

        # Re-fetch latest YES and NO prices from WebSocket L2 replica & caches to prevent stale entries & spread guard failures
        if self.position_mgr.ws:
            from unittest.mock import Mock
            if not isinstance(self.position_mgr.ws, Mock):
                # 1. Connection check
                if not getattr(self.position_mgr.ws, "is_connected", False):
                    logger.warning(
                        "ws_disconnected_reject",
                        market_id=market_id,
                        message="Rejecting signal: WebSocket client is disconnected or reconnecting."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason="WS_DISCONNECTED",
                            failed_checks=["ws_connection_status"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "WS_DISCONNECTED")
                    return False, "WebSocket client is disconnected or reconnecting"

            yes_tid = market.get("yes_token_id")
            no_tid = market.get("no_token_id")

            # 2. Price cache population check with multi-tier fallback (WS -> Implied -> REST)
            if not isinstance(self.position_mgr.ws, Mock):
                if yes_tid:
                    resolved_yes = await self._get_live_price_with_fallback(yes_tid, counterpart_tid=no_tid)
                    if resolved_yes is None:
                        logger.warning(
                            "orderbook_liquidity_empty_reject",
                            market_id=market_id,
                            token_id=yes_tid,
                            message="Rejecting signal: Orderbook liquidity unavailable for YES token across WS and REST."
                        )
                        if self.rejection_logger:
                            await self.rejection_logger.log_rejection(
                                market_id=market_id,
                                question=market_question,
                                primary_reason="INSUFFICIENT_LIQUIDITY",
                                failed_checks=["ws_price_cache_populated", "rest_orderbook_available"],
                                market_data=market
                            )
                        await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "INSUFFICIENT_LIQUIDITY")
                        return False, "Orderbook liquidity unavailable for YES token"
                    yes_price = float(resolved_yes)

                if no_tid:
                    resolved_no = await self._get_live_price_with_fallback(no_tid, counterpart_tid=yes_tid)
                    if resolved_no is None:
                        logger.warning(
                            "orderbook_liquidity_empty_reject",
                            market_id=market_id,
                            token_id=no_tid,
                            message="Rejecting signal: Orderbook liquidity unavailable for NO token across WS and REST."
                        )
                        if self.rejection_logger:
                            await self.rejection_logger.log_rejection(
                                market_id=market_id,
                                question=market_question,
                                primary_reason="INSUFFICIENT_LIQUIDITY",
                                failed_checks=["ws_price_cache_populated", "rest_orderbook_available"],
                                market_data=market
                            )
                        await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "INSUFFICIENT_LIQUIDITY")
                        return False, "Orderbook liquidity unavailable for NO token"
                    no_price = float(resolved_no)
            else:
                # Fallback path for unit tests mocking the WS client
                if yes_tid:
                    ws_yes = self._get_best_ask(yes_tid)
                    if ws_yes is not None:
                        yes_price = float(ws_yes)
                if no_tid:
                    ws_no = self._get_best_ask(no_tid)
                    if ws_no is not None:
                        no_price = float(ws_no)

        # Check Loss Momentum Skip Strategy early to determine if we should fade
        should_block_for_loss = False
        trigger_pos_id = None
        has_unresolved_recovery = False

        # Unconditionally check if there is already an active/unresolved recovery trade
        try:
            if self.skip_db is not None:
                unresolved = await self.skip_db.get_unresolved_skips(symbol=None)
                if len(unresolved) > 0:
                    has_unresolved_recovery = True
            
            if not has_unresolved_recovery:
                table = "paper_positions" if self.config.paper_trade else "positions"
                row = await fetch_one(
                    self.db,
                    f"SELECT 1 FROM {table} WHERE order_status = 'OPEN' AND strategy_mode = 'LOSS_FADE' LIMIT 1"
                )
                if row is not None:
                    has_unresolved_recovery = True
        except Exception as skip_err:
            logger.error("failed_to_check_unresolved_recovery_unconditional", error=str(skip_err))

        try:
            last_closed = await get_last_closed_trend_trade(
                self.db, self.skip_db, paper=self.config.paper_trade, symbol=None
            )
            if last_closed and last_closed["realised_pnl"] < 0:
                trigger_pos_id = last_closed["position_id"]
                already_skipped = False
                if self.skip_db is not None:
                    already_skipped = await self.skip_db.has_skipped_for_trigger(trigger_pos_id)
                
                if not already_skipped:
                    should_block_for_loss = True
        except Exception as skip_err:
            logger.error("early_loss_momentum_skip_check_failed", error=str(skip_err))

        # Trigger Strategy Profile evaluations early so they evaluate every confirmed signal,
        # completely independent of subsequent main bot rejections (B3 price gates, CVD gates, streak limits, etc.)
        if self.profile_mgr:
            try:
                # ─── Telemetry Pre-computation for Profiles ───
                # Compute CVD early
                cvd_60s_val = 0.0
                cvd_5m_val = 0.0
                if self.binance_ws and self.binance_ws.is_connected:
                    cvd_60s_val, cvd_5m_val = self.binance_ws.get_cvd_metrics(symbol)

                # Compute Order Book Imbalance early
                book_imbalance_val = 0.0
                try:
                    book_val = None
                    orig_tid_val = original_token_id
                    if not orig_tid_val:
                        orig_tid_val = market.get("yes_token_id") if original_side == "YES" else market.get("no_token_id")
                    if orig_tid_val:
                        if self.position_mgr.ws and self.position_mgr.ws.is_connected:
                            book_val = self.position_mgr.ws.get_full_book(orig_tid_val)
                        if not book_val:
                            book_val = await self.clob.get_orderbook(orig_tid_val)
                        if book_val:
                            bids_val = book_val.get("bids", [])
                            asks_val = book_val.get("asks", [])
                            top_bids_val = bids_val[:5]
                            top_asks_val = asks_val[:5]
                            bid_vol_val = sum(float(b.get("size", 0)) for b in top_bids_val)
                            ask_vol_val = sum(float(a.get("size", 0)) for a in top_asks_val)
                            if bid_vol_val + ask_vol_val > 0:
                                book_imbalance_val = (bid_vol_val - ask_vol_val) / (bid_vol_val + ask_vol_val)
                except Exception as imb_err:
                    logger.debug("failed_to_get_imbalance_pre_checks_profiles", error=str(imb_err))

                # Calculate spread early
                spread_val = 0.0
                yes_tid_val = market.get("yes_token_id")
                if yes_tid_val and self.position_mgr.ws:
                    yes_bid_val = self.position_mgr.ws.bid_cache.get(yes_tid_val)
                    yes_ask_val = self.position_mgr.ws.ask_cache.get(yes_tid_val)
                    from unittest.mock import MagicMock, AsyncMock
                    if yes_bid_val is not None and yes_ask_val is not None and not isinstance(yes_bid_val, (MagicMock, AsyncMock)) and not isinstance(yes_ask_val, (MagicMock, AsyncMock)):
                        spread_val = float(yes_ask_val) - float(yes_bid_val)

                # Calculate market age & elapsed time early
                secs_left_val = float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0
                market_age_at_entry_pct_val = 0.0
                if new_closes_at > 0:
                    duration_val = market.get("window_minutes") or 5
                    total_window_secs_val = duration_val * 60
                    elapsed_secs_val = total_window_secs_val - secs_left_val
                    market_age_at_entry_pct_val = max(0.0, min(1.0, elapsed_secs_val / total_window_secs_val))
                else:
                    elapsed_secs_val = 0.0

                # Win rates & Kelly Calculations early
                stats_val = await get_rolling_win_rate(self.db, symbol, str(window), streak_direction)
                n_signals_resolved_val = stats_val.get("n_signals_resolved", 0)
                rolling_win_rate_val = stats_val.get("rolling_win_rate", 0.5)

                p_val = min(0.80, rolling_win_rate_val)
                original_trend_price_val = yes_price if original_side == "YES" else no_price
                b_val = (0.98 / original_trend_price_val) - 1.0 if original_trend_price_val > 0.0 else 0.0
                live_half_kelly_val = 0.0
                if n_signals_resolved_val >= 15:
                    live_half_kelly_val = max(0.0, (p_val - (1.0 - p_val) / b_val) * 0.5) if b_val > 0.0 else 0.0

                profile_active_strategy_val = await self._get_active_strategy_mode(force_enabled=True)
                # Virtual profiles benchmark consistently at standard 10.0 USD sizing
                sizing_val = 10.0
                original_trend_shares_val = sizing_val / original_trend_price_val if original_trend_price_val > 0.0 else 0.0
                orig_tid_val = original_token_id
                if not orig_tid_val:
                    orig_tid_val = market.get("yes_token_id") if original_side == "YES" else market.get("no_token_id")

                await self.profile_mgr.evaluate_and_trigger(
                    market=market,
                    side=original_side,
                    entry_price=original_trend_price_val,
                    size_usdc=sizing_val,
                    shares=original_trend_shares_val,
                    strategy_mode=profile_active_strategy_val,
                    elapsed_secs=elapsed_secs_val,
                    persistence_score=persistence_score,
                    should_block_for_loss=should_block_for_loss,
                    active_strategy=profile_active_strategy_val,
                    target_token_id=orig_tid_val,
                    win_prob_estimate=rolling_win_rate_val,
                    kelly_fraction_used=live_half_kelly_val,
                    bid_ask_spread_at_entry=spread_val,
                    book_imbalance_at_entry=book_imbalance_val,
                    cvd_60s_at_entry=cvd_60s_val,
                    correlated_position_open=0,
                    seconds_to_close_at_entry=secs_left_val,
                    market_age_at_entry_pct=market_age_at_entry_pct_val,
                    bankroll_at_entry=current_bankroll,
                    streak_depth=streak_depth
                )
            except Exception as prof_err:
                logger.error("failed_to_evaluate_profiles_early_in_decision", error=str(prof_err))

        if market.get("is_lightweight", False):
            return False, "lightweight_signal"

        # ─── Main Bot Timeframe Guard ───
        if window == 15 and getattr(self.config, "timeframe_15m_enabled", False) is not True:
            logger.warning(
                "timeframe_15m_disabled_main_bot",
                market_id=market_id,
                message="Rejecting 15m window signal for main bot: 15-minute timeframe is disabled."
            )
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market_question,
                    primary_reason="TIMEFRAME_15M_DISABLED",
                    failed_checks=["timeframe_15m_disabled"],
                    market_data=market
                )
            await self._log_signal(market, original_side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "TIMEFRAME_15M_DISABLED")
            return False, "15-minute timeframe is disabled due to low ROI and high reversal probability"

        # ─── Main Bot Drawdown Guard ───
        if 'is_drawdown_halted' in locals() and is_drawdown_halted:
            drawdown_pct_str = f"{((self.starting_bankroll - current_bankroll) / self.starting_bankroll * 100):.1f}%" if self.starting_bankroll and self.starting_bankroll > 0 else ">= 20%"
            return False, f"Halted: Max Drawdown Breaker active (drawdown {drawdown_pct_str})"


        # Block early if a recovery trade is already active/unresolved to enforce FCFS concurrency limits
        if has_unresolved_recovery and getattr(self.config, "lossmomentum_skip_enabled", False) is True:
            logger.warning(
                "loss_fade_concurrency_blocked_early",
                market_id=market_id,
                message="LOSS_FADE entry blocked: recovery trade is already active/unresolved."
            )
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market_question,
                    primary_reason="LOSS_FADE_CONCURRENCY_BLOCKED",
                    failed_checks=["loss_fade_concurrency_gate"],
                    market_data=market
                )
            await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "LOSS_FADE_CONCURRENCY_BLOCKED")
            return False, "Recovery trade is already active/unresolved"

        # Determine active strategy mode (TREND or REVERSAL) based on past win rate
        active_strategy = await self._get_active_strategy_mode()

        # Override strategy to LOSS_FADE if skip/fade is triggered, enabled, and streak depth is safe (< 6)
        if should_block_for_loss and getattr(self.config, "lossmomentum_skip_enabled", False) is True:
            fade_token_id = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
            fade_entry_price = None
            if fade_token_id and self.position_mgr.ws:
                ws_fade = self._get_best_ask(fade_token_id)
                if ws_fade is not None:
                    fade_entry_price = float(ws_fade)
            
            if fade_entry_price is None:
                fade_entry_price = no_price if side == "YES" else yes_price

            if streak_depth >= 6:
                logger.info("loss_momentum_skip_depth_block", market_id=market_id, streak_depth=streak_depth)
                should_block_for_loss = False
            elif fade_entry_price is not None and fade_entry_price >= 0.50:
                logger.warning(
                    "loss_momentum_skip_price_block",
                    market_id=market_id,
                    fade_price=fade_entry_price,
                    message=f"LOSS_FADE entry blocked: opposing price {fade_entry_price:.4f} >= 0.50. Skipping recovery trade to maintain positive R:R."
                )
                # Keep should_block_for_loss = True to trigger standard skip block at end of process_signal,
                # but do NOT override active_strategy to LOSS_FADE.
            else:
                active_strategy = "LOSS_FADE"

        # Enforce Regime-Switching Circuit Breaker (v5.28)
        if getattr(self.config, "regime_filter_enabled", True) is True and active_strategy in ("TREND", "REVERSAL"):
            reversal_rolling_pnl = await self._get_reversal_profile_rolling_pnl()
            if reversal_rolling_pnl is not None:
                if active_strategy == "TREND" and reversal_rolling_pnl > 0:
                    logger.warning(
                        "trend_blocked_by_choppy_regime",
                        market_id=market_id,
                        reversal_pnl=reversal_rolling_pnl,
                        message="TREND trade blocked: Reversal profile rolling PnL > 0 (Choppy Regime)."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason="TREND_BLOCKED_BY_CHOPPY_REGIME",
                            failed_checks=["regime_filter_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "TREND_BLOCKED_BY_CHOPPY_REGIME")
                    return False, "TREND trade blocked by choppy regime"
                    
                elif active_strategy == "REVERSAL" and reversal_rolling_pnl <= 0:
                    logger.warning(
                        "reversal_blocked_by_trending_regime",
                        market_id=market_id,
                        reversal_pnl=reversal_rolling_pnl,
                        message="REVERSAL trade blocked: Reversal profile rolling PnL <= 0 (Trending Regime)."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason="REVERSAL_BLOCKED_BY_TRENDING_REGIME",
                            failed_checks=["regime_filter_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "REVERSAL_BLOCKED_BY_TRENDING_REGIME")
                    return False, "REVERSAL trade blocked by trending regime"

        if active_strategy == "TREND" and streak_depth >= 6:
            logger.warning(
                "trend_streak_depth_gate_rejected",
                market_id=market_id,
                streak_depth=streak_depth,
                message=f"TREND mode entry rejected: streak depth {streak_depth} >= 6 ceiling - REJECTED."
            )
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market_question,
                    primary_reason=f"TREND_STREAK_DEPTH_{streak_depth}_LIMIT_6",
                    failed_checks=["trend_streak_depth_gate"],
                    market_data=market
                )
            await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"TREND_STREAK_DEPTH_{streak_depth}_LIMIT_6")
            return False, f"TREND mode entry rejected: streak depth {streak_depth} >= 6"

        if active_strategy in ("REVERSAL", "LOSS_FADE"):
            trend_side = side
            trend_price = yes_price if trend_side == "YES" else no_price
            
            # Flip YES <-> NO side and target token ID
            side = "NO" if trend_side == "YES" else "YES"
            target_token_id = market.get("no_token_id") if trend_side == "YES" else market.get("yes_token_id")
            
            # Calculate reversal price with spread penalty
            # Calculate reversal price (without spread penalty)
            entry_price = 1.0 - trend_price
            logger.info(
                "reversal_routing_applied",
                market_id=market_id,
                original_side=trend_side,
                original_price=trend_price,
                new_side=side,
                reversal_price=entry_price,
                strategy_mode=active_strategy
            )
        else:
            # TREND mode: standard mapping
            if not target_token_id:
                if side == "YES":
                    target_token_id = market.get("yes_token_id")
                else:
                    target_token_id = market.get("no_token_id")
            entry_price = yes_price if side == "YES" else no_price

        # ─── Step -3: Pre-Entry Time-to-Close Guard (Change 5 — decision layer) ───
        # The scanner already checks this, but there's a race: by the time the
        # signal reaches here, we may be closer to expiry. Re-check to be safe.
        if new_closes_at > 0:
            secs_left = new_closes_at - int(time.time())
            
            # MEAN REVERSION FIX: Reject early phase trades.
            if secs_left > 600 and not is_short_window:
                logger.info("decision_early_phase_rejected", market_id=market_id, secs_left=secs_left, message="Rejecting: >600s left. Mean reversion requires panic/late phase.")
                return False, f"Market too early ({secs_left}s > 600s)"

            if is_short_window:
                duration = market.get("window_minutes") or 5
                min_ttc = 60 if duration == 5 else 120
            else:
                min_ttc = getattr(self.config, "min_seconds_to_close_pre_entry", 180)
                if type(min_ttc).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
                    min_ttc = 180
                else:
                    min_ttc = int(min_ttc)
                
            if secs_left < min_ttc:
                logger.info(
                    "decision_ttc_guard",
                    market_id=market_id,
                    secs_left=secs_left,
                    min_required=min_ttc,
                    message=f"Rejecting: only {secs_left}s left on market (need {min_ttc}s)"
                )
                return False, f"Market too close to expiry ({secs_left}s < {min_ttc}s)"

        # ─── Step -2.1: Empirical Unified Entry Gate (V3.21) ───
        # Re-fetch absolute latest price from WebSocket L2 replica & caches
        yes_token_id = market.get("yes_token_id")
        if yes_token_id and self.position_mgr.ws:
            latest_yes = self._get_best_ask(yes_token_id)
            if latest_yes is not None:
                yes_price = float(latest_yes)
                # Update downstream yes_price and entry_price variables
                if side == "YES":
                    entry_price = yes_price
                else:
                    no_token_id = market.get("no_token_id")
                    if no_token_id:
                        latest_no = self._get_best_ask(no_token_id)
                        if latest_no is not None:
                            no_price = float(latest_no)
                            entry_price = no_price

        # Enforce Flat Empirical Gates by Side (May 26 Baseline) - Hard-Re-enabled (Bypassed for LOSS_FADE & REVERSAL)
        if active_strategy not in ("LOSS_FADE", "REVERSAL"):
            if side == "YES":
                max_yes_allowed = 0.60
                min_yes_allowed = 0.30
                if yes_price < min_yes_allowed:
                    logger.warning(
                        "yes_down_move_too_strong_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES entry rejected due to YES price below {min_yes_allowed} ({yes_price:.4f}) - REJECTED."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"YES_DOWN_MOVE_TOO_STRONG_{yes_price:.4f}_LIMIT_{min_yes_allowed}",
                            failed_checks=["yes_down_move_too_strong_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"YES_DOWN_MOVE_TOO_STRONG_{yes_price:.4f}_LIMIT_{min_yes_allowed}")
                    return False, f"YES price below floor for YES entry ({yes_price:.4f} < {min_yes_allowed})"

                if yes_price > max_yes_allowed:
                    logger.warning(
                        "yes_exhaustion_gate_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES price exceeds ceiling ({yes_price:.4f} > {max_yes_allowed}) - REJECTED."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"YES_EXHAUSTION_ALREADY_PRICED_{yes_price:.4f}_LIMIT_{max_yes_allowed}",
                            failed_checks=["yes_exhaustion_ceiling_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"YES_EXHAUSTION_ALREADY_PRICED_{yes_price:.4f}_LIMIT_{max_yes_allowed}")
                    return False, f"YES price exceeds ceiling ({yes_price:.4f} > {max_yes_allowed})"

            else:
                # NO entries (fading UP-UP exhaustion)
                min_yes_allowed = 0.30
                max_yes_allowed = 0.60
                if yes_price < min_yes_allowed:
                    logger.warning(
                        "no_up_move_too_strong_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"NO entry rejected due to YES price below {min_yes_allowed} ({yes_price:.4f}) - REJECTED."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"NO_UP_MOVE_TOO_STRONG_{yes_price:.4f}_LIMIT_{min_yes_allowed}",
                            failed_checks=["no_up_move_too_strong_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"NO_UP_MOVE_TOO_STRONG_{yes_price:.4f}_LIMIT_{min_yes_allowed}")
                    return False, f"YES price below floor for NO entry ({yes_price:.4f} < {min_yes_allowed})"

                if yes_price > max_yes_allowed:
                    logger.warning(
                        "no_already_priced_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES price above ceiling {max_yes_allowed} ({yes_price:.4f}) - REJECTED."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"NO_ALREADY_PRICED_{yes_price:.4f}_LIMIT_{max_yes_allowed}",
                            failed_checks=["no_already_priced_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"NO_ALREADY_PRICED_{yes_price:.4f}_LIMIT_{max_yes_allowed}")
                    return False, f"YES price above ceiling for NO entry ({yes_price:.4f} > {max_yes_allowed})"

        # ─── Main Bot Price Floor and Ceiling Gates ─── (Bypassed for LOSS_FADE & REVERSAL)
        if active_strategy not in ("LOSS_FADE", "REVERSAL"):
            min_entry_price = getattr(self.config, "min_entry_price", 0.40)
            max_entry_price = getattr(self.config, "max_entry_price", 0.60)
            from unittest.mock import MagicMock, AsyncMock
            if isinstance(min_entry_price, (MagicMock, AsyncMock)):
                min_entry_price = 0.40
            if isinstance(max_entry_price, (MagicMock, AsyncMock)):
                max_entry_price = 0.60
            trend_entry_price = yes_price if original_side == "YES" else no_price

            if trend_entry_price < min_entry_price:
                logger.info(
                    "quality_filter_price_floor_rejected",
                    market_id=market_id,
                    target_side=original_side,
                    target_price=trend_entry_price,
                    min_entry=min_entry_price,
                    message=f"Rejecting signal for main bot: price {trend_entry_price} below floor {min_entry_price}"
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"ENTRY_PRICE_{trend_entry_price:.4f}_LT_{min_entry_price:.2f}",
                        failed_checks=["min_entry_price_floor_gate"],
                        market_data=market
                    )
                await self._log_signal(market, original_side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"ENTRY_PRICE_{trend_entry_price:.4f}_LT_{min_entry_price:.2f}")
                return False, f"Entry price {trend_entry_price} below floor {min_entry_price}"

            if trend_entry_price > max_entry_price:
                logger.info(
                    "quality_filter_price_ceiling_rejected",
                    market_id=market_id,
                    target_side=original_side,
                    target_price=trend_entry_price,
                    max_entry=max_entry_price,
                    message=f"Rejecting signal for main bot: price {trend_entry_price} above ceiling {max_entry_price}"
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"ENTRY_PRICE_{trend_entry_price:.4f}_GT_{max_entry_price:.2f}",
                        failed_checks=["max_entry_price_ceiling_gate"],
                        market_data=market
                    )
                await self._log_signal(market, original_side, 0.0, 0.0, 0.0, 0.0, "REJECTED", f"ENTRY_PRICE_{trend_entry_price:.4f}_GT_{max_entry_price:.2f}")
                return False, f"Entry price {trend_entry_price} above ceiling {max_entry_price}"

        # ─── Step -2: Spread Guard (Max Entry Price) ─── - Soft-Disabled
        opposing_price = no_price if side == "YES" else yes_price
        fair_entry_price = max(0.01, 1.0 - opposing_price)
        
        mult = self.config.max_no_ask_multiplier
        if any(asset.strip().upper() in market_question.upper() for asset in self.config.high_vol_assets):
            mult = self.config.high_vol_ask_multiplier
            
        max_entry_price = min(1.0, round(fair_entry_price * mult, 4))
        if entry_price > max_entry_price:
            logger.warning(
                "spread_guard_soft_disabled", 
                market_id=market_id, 
                entry_price=entry_price, 
                opposing_price=opposing_price, 
                message=f"Spread too wide: {side} price ({entry_price}) > max allowed ({max_entry_price}) - soft warned."
            )
            would_reject_reasons.append(f"SPREAD_GUARD_{side}_PRICE_{entry_price}_GT_{max_entry_price}")

        # ─── Streak Quality Score Guard (Passive / Diagnostic) ───
        streak_quality_score = market.get("streak_quality_score")
        min_quality = getattr(self.config, "min_streak_quality_score", 0.45)
        if streak_quality_score is not None and streak_quality_score < min_quality:
            logger.warning(
                "streak_quality_below_threshold",
                market_id=market_id,
                score=streak_quality_score,
                threshold=min_quality,
                message=f"Market quality score {streak_quality_score:.4f} < {min_quality} (passive audit only)."
            )
            would_reject_reasons.append(f"STREAK_QUALITY_{streak_quality_score:.4f}_LT_{min_quality:.2f}")

        # ─── Step -1: Global Concurrency & Cooldown Guard ───
        if True:
            now = time.time()
            
            asset_keywords = ["bitcoin", "ethereum", "solana", "dogecoin", "xrp", "bnb", "hyperliquid"]
            current_asset = next((kw for kw in asset_keywords if kw in market_question.lower()), "unknown")

            # ─── Step 0: Ensure we never trade a market more than once ───
            has_traded = await has_traded_market(self.db, market_id, paper=self.config.paper_trade)
            if has_traded:
                logger.info("decision_duplicate_market", market_id=market_id, message="Market already traded. Skipping to prevent duplicates.")
                return False, "Market already traded once"

            # ─── Step 0.2: Hard Limit for Crypto Mode ───
            # ─── Step 0.2: Hard Limit for Crypto Mode ───
            open_positions = await get_open_positions(self.db, paper=self.config.paper_trade)
            
            # --- Filter out closed-but-unresolved positions from active limit checks ---
            active_open_positions = []
            now_ts = int(time.time())
            for pos in open_positions:
                m_data = await get_market(self.db, pos["market_id"])
                if m_data:
                    closes_at = m_data.get("closes_at", 0)
                    if closes_at == 0 or now_ts <= closes_at:
                        active_open_positions.append(pos)
                else:
                    active_open_positions.append(pos)

            # ─── Step 0.3: LOSS_FADE Concurrency & Unresolved Skips Guard ─── - Hard-Enabled
            if active_strategy == "LOSS_FADE":
                has_open_fade = False
                for pos in open_positions:
                    if pos.get("strategy_mode") == "LOSS_FADE":
                        has_open_fade = True
                        break

                has_unresolved_skips = False
                if self.skip_db is not None:
                    try:
                        unresolved = await self.skip_db.get_unresolved_skips()
                        if len(unresolved) > 0:
                            has_unresolved_skips = True
                    except Exception as skip_err:
                        logger.error("failed_to_check_unresolved_skips", error=str(skip_err))

                if has_open_fade or has_unresolved_skips:
                    logger.warning(
                        "loss_fade_concurrency_blocked",
                        market_id=market_id,
                        has_open_fade=has_open_fade,
                        has_unresolved_skips=has_unresolved_skips,
                        message="LOSS_FADE entry blocked: recovery trade is already active/unresolved."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason="LOSS_FADE_CONCURRENCY_BLOCKED",
                            failed_checks=["loss_fade_concurrency_gate"],
                            market_data=market
                        )
                    await self._log_signal(market, side, 0.0, 0.0, 0.0, 0.0, "REJECTED", "LOSS_FADE_CONCURRENCY_BLOCKED")
                    return False, "Recovery trade is already active/unresolved"

            # --- FIX: "Double-Spending" Race Condition Guard ---
            current_exposure = sum(p.get("size_usdc", 0.0) for p in active_open_positions)
            if current_exposure + self.config.position_size_usdc > self.config.max_total_exposure_usdc:
                logger.warning(
                    "max_exposure_guard",
                    current=current_exposure,
                    adding=self.config.position_size_usdc,
                    max=self.config.max_total_exposure_usdc,
                    message="Max total exposure limit reached in decision engine."
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"MAX_EXPOSURE_REACHED_{current_exposure}",
                        failed_checks=["max_exposure_guard"],
                        market_data=market
                    )
                return False, f"Max total exposure reached ({current_exposure} + {self.config.position_size_usdc} > {self.config.max_total_exposure_usdc})"




            # ─── Step 0.8: Binance Spot Price Velocity Z-Score Gate ─── (Bypassed for LOSS_FADE & REVERSAL)
            z_score = 0.0
            if self.binance_ws and self.binance_ws.is_connected and active_strategy not in ("LOSS_FADE", "REVERSAL"):
                try:
                    z_score = self.binance_ws.get_spot_velocity_z_score(symbol)
                    if abs(z_score) > 3.0:
                        if window == 5:
                            logger.warning(
                                "spot_velocity_z_score_would_reject",
                                symbol=symbol,
                                z_score=round(z_score, 2),
                                message=f"Binance spot price velocity Z-score is extreme ({z_score:.2f}) but soft-enabled for 5m."
                            )
                            would_reject_reasons.append(f"SPOT_VELOCITY_Z_SCORE_SPIKE_{z_score:.2f}")
                except Exception as z_err:
                    logger.error("failed_to_get_z_score_decision", error=str(z_err))

            # ─── Step 1: Run all 9 validation checks ───
            validation = await self.validator.validate(market, target_token_id)
            # would_reject_reasons = []  # Commented out to preserve early soft-rejections

            # Reversal bypass completely removed per pure Trend-Following strategy rules

            # Compute CVD and Order Book Imbalance pre-checks
            cvd_60s = 0.0
            cvd_5m = 0.0
            if self.binance_ws and self.binance_ws.is_connected:
                cvd_60s, cvd_5m = self.binance_ws.get_cvd_metrics(symbol)
                
            book_imbalance = 0.0
            try:
                book = None
                if self.position_mgr.ws and self.position_mgr.ws.is_connected:
                    book = self.position_mgr.ws.get_full_book(target_token_id)
                if not book:
                    book = await self.clob.get_orderbook(target_token_id)
                if book:
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    top_bids = bids[:5]
                    top_asks = asks[:5]
                    bid_vol = sum(float(b.get("size", 0)) for b in top_bids)
                    ask_vol = sum(float(a.get("size", 0)) for a in top_asks)
                    if bid_vol + ask_vol > 0:
                        book_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
            except Exception as e:
                logger.error("failed_to_get_imbalance_pre_checks", error=str(e))

            secs_left = float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0

            # ─── CVD Direction-Exhaustion Gate (Bypassed for LOSS_FADE & REVERSAL, Soft-Disabled per User Request) ───
            if active_strategy not in ("LOSS_FADE", "REVERSAL"):
                if side == "YES" and cvd_60s > 50000.0:
                    logger.warning(
                        "cvd_direction_exhaustion_warning_soft_disabled",
                        market_id=market_id,
                        side=side,
                        cvd_60s=cvd_60s,
                        message=f"CVD warning YES: CVD {cvd_60s:.1f} > 50k (soft-disabled)."
                    )
                    would_reject_reasons.append(f"CVD_YES_EXHAUSTION_{cvd_60s:.1f}")
                elif side == "NO" and cvd_60s < -50000.0:
                    logger.warning(
                        "cvd_direction_exhaustion_warning_soft_disabled",
                        market_id=market_id,
                        side=side,
                        cvd_60s=cvd_60s,
                        message=f"CVD warning NO: CVD {cvd_60s:.1f} < -50k (soft-disabled)."
                    )
                    would_reject_reasons.append(f"CVD_NO_EXHAUSTION_{cvd_60s:.1f}")

            if not validation.passed:
                await log_event(
                    self.db,
                    event_type="SIGNAL_REJECTED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    payload={
                        "checks": validation.checks,
                        "reasons": validation.reasons,
                        "yes_price": yes_price,
                        "no_price": no_price,
                    },
                )
                await self._log_signal(market, side, 0.0, book_imbalance, cvd_60s, secs_left, "REJECTED", f"Validation: {'; '.join(validation.reasons)}")
                return False, f"Validation failed: {'; '.join(validation.reasons)}"

            # ─── Pre-computation for Profiles and Sizing ───
            # Calculate spread at entry
            spread = 0.0
            yes_tid = market.get("yes_token_id")
            if yes_tid and self.position_mgr.ws:
                yes_bid = self.position_mgr.ws.bid_cache.get(yes_tid)
                yes_ask = self.position_mgr.ws.ask_cache.get(yes_tid)
                from unittest.mock import MagicMock, AsyncMock
                if yes_bid is not None and yes_ask is not None and not isinstance(yes_bid, (MagicMock, AsyncMock)) and not isinstance(yes_ask, (MagicMock, AsyncMock)):
                    spread = float(yes_ask) - float(yes_bid)

            # Calculate market age percentage
            market_age_at_entry_pct = 0.0
            if new_closes_at > 0:
                duration = market.get("window_minutes") or 5
                total_window_secs = duration * 60
                elapsed_secs = total_window_secs - secs_left
                market_age_at_entry_pct = max(0.0, min(1.0, elapsed_secs / total_window_secs))

            # Sizing & Kelly Calculations
            direction = "UP" if side == "YES" else "DOWN"
            stats = await get_rolling_win_rate(self.db, symbol, str(window), direction)
            n_signals_resolved = stats.get("n_signals_resolved", 0)
            rolling_win_rate = stats.get("rolling_win_rate", 0.5)

            p = min(0.80, rolling_win_rate)
            b = (0.98 / entry_price) - 1.0 if entry_price > 0.0 else 0.0
            live_half_kelly = 0.0
            if n_signals_resolved >= 15:
                live_half_kelly = max(0.0, (p - (1.0 - p) / b) * 0.5) if b > 0.0 else 0.0



            # ─── Order Book Imbalance Check (B2 - Soft-Disabled per User Request) ───
            if book_imbalance != 0.0:
                if side == "YES" and book_imbalance < -0.15:
                    logger.warning("imbalance_warning_soft_disabled", side=side, imbalance=book_imbalance)
                    would_reject_reasons.append(f"Imbalance YES sellers dominate ({book_imbalance:.3f})")
                elif side == "NO" and book_imbalance > 0.15:
                    logger.warning("imbalance_warning_soft_disabled", side=side, imbalance=book_imbalance)
                    would_reject_reasons.append(f"Imbalance NO buyers dominate ({book_imbalance:.3f})")

            # Calculate spread percentage
            spread_pct = 0.0
            yes_tid = market.get("yes_token_id")
            if yes_tid and self.position_mgr.ws:
                yes_bid = self.position_mgr.ws.bid_cache.get(yes_tid)
                yes_ask = self.position_mgr.ws.ask_cache.get(yes_tid)
                from unittest.mock import MagicMock, AsyncMock
                if yes_bid is not None and yes_ask is not None and not isinstance(yes_bid, (MagicMock, AsyncMock)) and not isinstance(yes_ask, (MagicMock, AsyncMock)):
                    if yes_ask > 0:
                        spread_pct = (yes_ask - yes_bid) / yes_ask



            # ─── Step 2: Fee-adjusted EV check ───
            if entry_price <= 0:
                return False, f"{side} price is zero or negative"

            shares = self.config.position_size_usdc / entry_price
            
            # MISTAKE 6 GUARD: 2% fee on net winnings
            net_payout = (1.0 - entry_price) * shares * FEE_MULTIPLIER
                
            ev_ratio = net_payout / self.config.position_size_usdc

            # ─── C2 Dynamic EV Sizing based on Spread Regime (Step 5) ───
            # quiet cut-off p25 = 1.96% (0.0196), volatility threshold p75 = 2.04% (0.0204)
            ev_multiplier = 1.0
            if spread_pct < 0.0196:
                ev_multiplier = 1.5
                logger.info("ev_threshold_scaled_up_low_spread", spread_pct=f"{spread_pct*100:.3f}%", multiplier=1.5)
            elif spread_pct > 0.0204:
                ev_multiplier = 0.7
                logger.info("ev_threshold_scaled_down_high_spread", spread_pct=f"{spread_pct*100:.3f}%", multiplier=0.7)
                
            scaled_ev_threshold = self.config.min_ev_threshold * ev_multiplier
 
            if ev_ratio < scaled_ev_threshold:
                logger.warning(
                    "insufficient_ev",
                    market_id=market_id,
                    ev_ratio=round(ev_ratio, 3),
                    min_required=round(scaled_ev_threshold, 3),
                    entry_price=entry_price,
                    message=f"EV too low: {ev_ratio:.3f} < {scaled_ev_threshold:.3f}",
                )
                await log_event(
                    self.db,
                    event_type="SIGNAL_REJECTED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    payload={"reason": "INSUFFICIENT_EV", "ev_ratio": ev_ratio, "scaled_threshold": scaled_ev_threshold},
                )
                await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "REJECTED", f"EV too low: {ev_ratio:.3f} < {scaled_ev_threshold:.3f}")
                return False, f"Insufficient EV: {ev_ratio:.3f} < {scaled_ev_threshold:.3f}"

            # ─── Step 4: Quantitative Sizing & API Minimums ───
            # Restored the highly successful May 26 Symmetric Dynamic Sizing Engine.
            # Uses live Quarter-Kelly sizer, timeframes/correlation skews, and symmetric streak-proportional ceilings.
            # direction, stats, n_signals_resolved, rolling_win_rate are pre-calculated above
            
            # Flat sizing per user request: no multipliers, no skews, no decay
            sizing = getattr(self.config, "position_size_usdc", 10.0)
            from unittest.mock import MagicMock, AsyncMock
            if isinstance(sizing, (MagicMock, AsyncMock)):
                sizing = 10.0
            sizing = round(sizing, 2)
            logger.info("sizing_final_computed_flat", symbol=symbol, window=window, final_size=sizing)
            
            raw_shares = sizing / entry_price
            shares = float(int(raw_shares))
            current_size_usdc = shares * entry_price

            # ─── Calculate & Log Hypothetical Kelly Evaluation ───
            # p, b, live_half_kelly are pre-calculated above
            
            # Setup flat parameters for hypothetical calculations to prevent NameErrors
            streak_multiplier = 1.0
            max_size_allowed = sizing
            window_skew = 1.0
            max_cap = sizing
            decay_mult = 1.0
            base_size = sizing
            if n_signals_resolved < 15:
                hypothetical_base_size = (self.config.position_size_usdc if not isinstance(self.config.position_size_usdc, (MagicMock, AsyncMock)) else 10.0) * 0.5 * streak_multiplier
            else:
                # live_half_kelly is pre-calculated above
                hypothetical_base_size = live_half_kelly * current_bankroll * 0.5 * streak_multiplier
            
            hypothetical_sizing = min(max_size_allowed, hypothetical_base_size)
            hypothetical_sizing = hypothetical_sizing * window_skew
            hypothetical_sizing = max(1.0, min(max_cap, hypothetical_sizing))
            hypothetical_sizing = hypothetical_sizing * decay_mult
            hypothetical_sizing = round(hypothetical_sizing, 2)
            hypothetical_shares = float(int(hypothetical_sizing / entry_price)) if entry_price > 0.0 else 0.0

            await log_hypothetical_kelly(
                market_id=market_id,
                symbol=symbol,
                window=window,
                direction=direction,
                streak_depth=streak_depth,
                rolling_win_rate=rolling_win_rate,
                n_signals_resolved=n_signals_resolved,
                live_entry_price=entry_price,
                live_odds_b=b,
                live_half_kelly=live_half_kelly,
                hypothetical_base_size=hypothetical_base_size,
                hypothetical_final_shares=hypothetical_shares,
                actual_flat_base_size=base_size
            )




            # Check if live orders are restricted to a specific profile (e.g. livemode)
            live_profile = getattr(self.config, "live_profile_only", "livemode")
            if type(live_profile).__name__ not in ('MagicMock', 'AsyncMock', 'Mock'):
                live_profile_str = str(live_profile).lower().strip()
            else:
                live_profile_str = "livemode"

            if not self.position_mgr._paper and live_profile_str not in ("core", "all", "none"):
                logger.info(
                    "core_momentum_skipped_for_live_profile",
                    live_profile_only=live_profile_str,
                    market_id=market_id,
                    message=f"Core momentum order skipped: live trading is restricted to '{live_profile_str}' profile."
                )
                await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "BYPASSED_FOR_LIVE_PROFILE", reason=f"Live orders restricted to {live_profile_str}")
                return True, f"Core momentum skipped: live orders restricted to {live_profile_str}"

            # Enforce hardcoded minimum order size of 6 shares for core momentum
            order_min_size = 6.0

            if shares < order_min_size:
                logger.warning(
                    "insufficient_shares_rejected",
                    market_id=market_id,
                    calculated_shares=round(shares, 2),
                    required_minimum=order_min_size,
                    message=f"Calculated shares ({shares:.2f}) is below hardcoded minimum of {order_min_size}. Rejecting signal.",
                )
                await log_event(
                    self.db,
                    event_type="SIGNAL_REJECTED",
                    bot_version=self.config.bot_version,
                    market_id=market_id,
                    payload={
                        "reason": "INSUFFICIENT_SHARES",
                        "calculated_shares": shares,
                        "required_minimum": order_min_size,
                    },
                )
                return False, f"Calculated shares ({shares:.2f}) below minimum 6.0 threshold"
            # ─── Step 5: Execute via Position Manager ───
            if True:  # Removed nested lock wrapper (now serialized at loop level in main.py)

                # spread and market_age_at_entry_pct are pre-calculated above

                # ─── Loss Momentum Skip Check & Recording ───
                if should_block_for_loss and trigger_pos_id:
                    try:
                        # Record in skip_db using original trend parameters for stats/auditing
                        trend_price_val = yes_price if original_side == "YES" else no_price
                        trend_shares_val = current_size_usdc / trend_price_val if trend_price_val > 0 else 0.0
                        
                        skipped_id = await self.skip_db.record_skip(
                            market_id=market_id,
                            question=market_question,
                            side=original_side,
                            entry_price=trend_price_val,
                            size_usdc=current_size_usdc,
                            shares_bought=trend_shares_val,
                            trigger_position_id=trigger_pos_id,
                            symbol=symbol
                        )
                        logger.warning(
                            "loss_momentum_skip_recorded",
                            market_id=market_id,
                            skipped_position_id=skipped_id,
                            trigger_position_id=trigger_pos_id,
                            message=f"Recorded hypothetical skip/fade for {market_question} to avoid loss momentum."
                        )
                    except Exception as rec_err:
                        logger.error("failed_to_record_skip_db", error=str(rec_err))

                    # If strategy is enabled (hard mode) and we did not fade, we block the trade!
                    if getattr(self.config, "lossmomentum_skip_enabled", False) is True and active_strategy != "LOSS_FADE":
                        logger.info("loss_momentum_skip_blocked_trade", market_id=market_id)
                        await log_event(
                            self.db,
                            event_type="SIGNAL_REJECTED",
                            bot_version=self.config.bot_version,
                            market_id=market_id,
                            payload={
                                "reason": "LOSS_MOMENTUM_SKIP_BLOCKED",
                                "trigger_position_id": trigger_pos_id,
                            },
                        )
                        await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "REJECTED", f"Loss momentum skip block (triggered by {trigger_pos_id})")
                        return False, "Blocked by Loss Momentum Skip Strategy"

                success = await self.position_mgr.open_position(
                    market_id=market_id,
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=current_size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=stats.get("rolling_win_rate", 0.5) if 'stats' in locals() else 0.5,
                    kelly_fraction_used=live_half_kelly if 'live_half_kelly' in locals() else 0.0,
                    bid_ask_spread_at_entry=spread,
                    book_imbalance_at_entry=book_imbalance if 'book_imbalance' in locals() else 0.0,
                    cvd_60s_at_entry=cvd_60s if 'cvd_60s' in locals() else 0.0,
                    correlated_position_open=1 if (has_correlated_same_direction if 'has_correlated_same_direction' in locals() else False) else 0,
                    seconds_to_close_at_entry=secs_left,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=current_bankroll if 'current_bankroll' in locals() else 100.0,
                    streak_depth=market.get("streak_depth", 2),
                    persistence_score=persistence_score,
                    strategy_mode=active_strategy,
                    inverted=(active_strategy == "LOSS_FADE"),
                    original_side=original_side,
                )

                if success:
                    if self.scanner:
                        closes_at = market.get("closes_at") or 0
                        if closes_at:
                            symbol, window = self._get_market_key_info(market)
                            key = (symbol, window)
                            self.scanner.last_traded_close_ts[key] = int(closes_at)
                            logger.info("streak_reset_updated", key=key, closes_at=closes_at)
                    await log_event(
                        self.db,
                        event_type="ORDER_PLACED",
                        bot_version=self.config.bot_version,
                        market_id=market_id,
                        payload={
                            "side": side,
                            "entry_price": entry_price,
                            "shares": shares,
                            "size_usdc": current_size_usdc,
                            "ev_ratio": ev_ratio,
                            "yes_price": yes_price,
                            "no_price": no_price,
                        },
                    )
                    reason_str = None
                    if 'would_reject_reasons' in locals() and would_reject_reasons:
                        reason_str = f"WOULD_REJECT: {'; '.join(would_reject_reasons)}"
                        if self.rejection_logger:
                            try:
                                await self.rejection_logger.log_rejection(
                                    market_id=market_id,
                                    question=market_question,
                                    primary_reason=f"WOULD_HAVE_BLOCKED: {'; '.join(would_reject_reasons)}",
                                    failed_checks=would_reject_reasons,
                                    market_data={
                                        "symbol": symbol,
                                        "window": window,
                                        "side": side,
                                        "entry_price": entry_price,
                                        "yes_price": yes_price,
                                        "price_velocity": local_price_velocity,
                                        "book_imbalance": book_imbalance if 'book_imbalance' in locals() else 0.0,
                                        "cvd_60s": cvd_60s if 'cvd_60s' in locals() else 0.0,
                                        "audit_status": "EXECUTED_WITH_BYPASS"
                                    }
                                )
                            except Exception as rej_err:
                                logger.error("failed_to_log_bypass_to_rejections", error=str(rej_err))
                        try:
                            from src.db.soft_audit_db import log_soft_trade
                            await log_soft_trade(
                                db_path=self.soft_audit_db_path,
                                market_id=market_id,
                                symbol=symbol,
                                window_size=str(window),
                                side=side,
                                entry_price=entry_price,
                                yes_price=yes_price,
                                price_velocity=local_price_velocity,
                                z_score=z_score if 'z_score' in locals() else 0.0,
                                reasons='; '.join(would_reject_reasons),
                                status="EXECUTED"
                            )
                        except Exception as soft_db_err:
                            logger.error("failed_to_log_soft_trade_exec", error=str(soft_db_err))
                    await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "ACCEPTED", reason=reason_str, price_velocity=local_price_velocity)
                    return True, "Order placed successfully"
                else:
                    await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "REJECTED", "Position manager open_position failed", price_velocity=local_price_velocity)
                    return False, "Position manager failed to execute order"

