import re
import asyncio
from typing import Any, Dict, Optional, Tuple
import time

from config.config import BotConfig
from src.strategy.math_utils import expected_value, slippage_depth_required, FEE_MULTIPLIER, calculate_kelly_fraction, get_linear_time_decay_multiplier, calculate_correlated_penalty_multiplier
from src.api.clob_client import ClobClient
from src.strategy.validator import SignalValidator, ValidationResult
from src.strategy.position_mgr import PositionManager
from src.db.queries import log_event, has_traded_market, get_open_positions, get_market, get_rolling_win_rate, log_signal_evaluation, get_historical_yes_price
from src.db.rejections import RejectionLogger
from src.logs.log_config import get_logger

logger = get_logger("decision")

# Global Entry Lock and Timestamp to prevent rapid-fire burst entries
_global_entry_lock = asyncio.Lock()
_last_entry_time = 0.0

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
    ):
        self.config = config
        self.validator = validator
        self.position_mgr = position_mgr
        self.clob = clob_client
        self.db = db
        self.rejection_logger = rejection_logger
        self.scanner = scanner
        self.binance_ws = binance_ws
        self._evaluated_markets = set()
        self._last_seen_scan_count = 0
        self.starting_bankroll = None
        self.drawdown_halted = False

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
        price_velocity: float = 0.0
    ) -> None:
        try:
            symbol, window = self.scanner._get_market_key_info(market) if self.scanner else ("BTC", 5)
            direction = "UP" if side == "NO" else "DOWN"
            yes_price = market.get("yes_price", 0.0)
            
            yes_tid = market.get("yes_token_id")
            spread = 0.0
            if yes_tid and self.position_mgr.ws:
                yes_bid = self.position_mgr.ws.bid_cache.get(yes_tid)
                yes_ask = self.position_mgr.ws.ask_cache.get(yes_tid)
                if yes_bid and yes_ask:
                    spread = yes_ask - yes_bid
                    
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
        global _last_entry_time
        local_price_velocity = 0.0
        
        market_id = market["market_id"]
        market_question = market.get("question", "")
        
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
        
        # ─── Max Drawdown Breaker Check ───
        if self.drawdown_halted:
            return False, "Halted: Max Drawdown Breaker active (drawdown >= 20%)"
            
        if self.starting_bankroll is None:
            # Initialize starting bankroll
            if self.config.paper_trade:
                cursor = await self.db.execute("SELECT SUM(realised_pnl) FROM paper_positions WHERE order_status = 'FILLED'")
                row = await cursor.fetchone()
                realised_sum = row[0] if row and row[0] is not None else 0.0
                self.starting_bankroll = max(10.0, 10000.0 + realised_sum)
            else:
                try:
                    bal_data = await self.clob.get_account_balance()
                    self.starting_bankroll = float(bal_data["balance"]) if (bal_data and "balance" in bal_data) else 100.0
                except Exception:
                    self.starting_bankroll = 100.0
            logger.info("starting_bankroll_initialized", starting_bankroll=self.starting_bankroll)
            
        # Compute current bankroll and check drawdown
        if self.config.paper_trade:
            cursor = await self.db.execute("SELECT SUM(realised_pnl) FROM paper_positions WHERE order_status = 'FILLED'")
            row = await cursor.fetchone()
            realised_sum = row[0] if row and row[0] is not None else 0.0
            current_bankroll = max(10.0, 10000.0 + realised_sum)
        else:
            try:
                bal_data = await self.clob.get_account_balance()
                current_bankroll = float(bal_data["balance"]) if (bal_data and "balance" in bal_data) else 100.0
            except Exception:
                current_bankroll = 100.0

                
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
                return False, f"Halted: Max Drawdown Breaker active ({drawdown*100:.1f}% >= 20%)"
                
        symbol, window = self.scanner._get_market_key_info(market) if self.scanner else ("BTC", 5)
        streak_depth = market.get("streak_depth", 2)
        is_reversal = (streak_depth >= 3) if window == 5 else (streak_depth >= 2)
        q_lower = market_question.lower()
        is_short_window = "up or down" in q_lower and any(kw in q_lower for kw in ["bitcoin", "btc", "ethereum", "eth"])
        yes_price = market.get("yes_price", 0.0)
        no_price = market.get("no_price", 0.0)
        new_closes_at = market.get("closes_at") or 0

        # Determine the target token ID dynamically if not provided
        if not target_token_id:
            if side == "YES":
                target_token_id = market.get("yes_token_id")
            else:
                target_token_id = market.get("no_token_id")

        # Re-fetch latest YES and NO prices from WebSocket cache if available to prevent stale entries & spread guard failures
        if self.position_mgr.ws:
            yes_tid = market.get("yes_token_id")
            no_tid = market.get("no_token_id")
            if yes_tid:
                ws_yes = self.position_mgr.ws.ask_cache.get(yes_tid) or self.position_mgr.ws.price_cache.get(yes_tid)
                if ws_yes is not None:
                    yes_price = float(ws_yes)
            if no_tid:
                ws_no = self.position_mgr.ws.ask_cache.get(no_tid) or self.position_mgr.ws.price_cache.get(no_tid)
                if ws_no is not None:
                    no_price = float(ws_no)

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
        # Re-fetch absolute latest price from WebSocket cache
        yes_token_id = market.get("yes_token_id")
        if yes_token_id and self.position_mgr.ws:
            latest_yes = self.position_mgr.ws.ask_cache.get(yes_token_id) or self.position_mgr.ws.price_cache.get(yes_token_id)
            if latest_yes is not None:
                yes_price = float(latest_yes)
                # Update downstream yes_price and entry_price variables
                if side == "YES":
                    entry_price = yes_price
                else:
                    no_token_id = market.get("no_token_id")
                    if no_token_id:
                        latest_no = self.position_mgr.ws.ask_cache.get(no_token_id) or self.position_mgr.ws.price_cache.get(no_token_id)
                        if latest_no is not None:
                            no_price = float(latest_no)
                            entry_price = no_price

        # Enforce Flat Empirical Gates by Side (May 26 Baseline)
        if not is_reversal:
            if side == "YES":
                max_yes_allowed = 0.52
                # YES entries (fading DOWN-DOWN exhaustion)
                if yes_price > max_yes_allowed:
                    logger.warning(
                        "yes_exhaustion_gate_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES exhaustion already priced ({yes_price:.4f} > {max_yes_allowed}). Edge gone."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"YES_EXHAUSTION_ALREADY_PRICED_{yes_price:.4f}_LIMIT_{max_yes_allowed}",
                            failed_checks=["yes_exhaustion_gate"],
                            market_data=market
                        )
                    # Log signal rejection to db
                    await self._log_signal(
                        market, 
                        side, 
                        0.0, 
                        0.0, 
                        0.0, 
                        float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                        "REJECTED", 
                        f"YES_EXHAUSTION_ALREADY_PRICED: {yes_price:.4f} (limit {max_yes_allowed})"
                    )
                    return False, f"YES_EXHAUSTION_ALREADY_PRICED: {yes_price:.4f} (limit {max_yes_allowed})"

                if yes_price < 0.38:
                    logger.warning(
                        "yes_falling_knife_floor_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES price below hard floor ({yes_price:.4f} < 0.38). DOWN move too strong (falling knife)."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"YES_FALLING_KNIFE_{yes_price:.4f}",
                            failed_checks=["yes_falling_knife_gate"],
                            market_data=market
                        )
                    # Log signal rejection to db
                    await self._log_signal(
                        market, 
                        side, 
                        0.0, 
                        0.0, 
                        0.0, 
                        float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                        "REJECTED", 
                        f"YES_FALLING_KNIFE: {yes_price:.4f}"
                    )
                    return False, f"YES_FALLING_KNIFE: {yes_price:.4f}"
            else:
                # NO entries (fading UP-UP exhaustion)
                min_yes_allowed = 0.42
                if yes_price < min_yes_allowed:
                    logger.warning(
                        "no_up_move_too_strong_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES price below {min_yes_allowed} ({yes_price:.4f}). UP move too strong, NO price too expensive."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"NO_UP_MOVE_TOO_STRONG_{yes_price:.4f}_LIMIT_{min_yes_allowed}",
                            failed_checks=["no_up_move_too_strong_gate"],
                            market_data=market
                        )
                    # Log signal rejection to db
                    await self._log_signal(
                        market, 
                        side, 
                        0.0, 
                        0.0, 
                        0.0, 
                        float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                        "REJECTED", 
                        f"NO_UP_MOVE_TOO_STRONG: {yes_price:.4f} (limit {min_yes_allowed})"
                    )
                    return False, f"NO_UP_MOVE_TOO_STRONG: {yes_price:.4f} (limit {min_yes_allowed})"

                if yes_price > 0.62:
                    logger.warning(
                        "no_already_priced_rejected",
                        market_id=market_id,
                        yes_price=yes_price,
                        message=f"YES price above 0.62 ({yes_price:.4f}). NO price cheap but trap (CHEAP NO trap)."
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"NO_ALREADY_PRICED_{yes_price:.4f}",
                            failed_checks=["no_already_priced_gate"],
                            market_data=market
                        )
                    # Log signal rejection to db
                    await self._log_signal(
                        market, 
                        side, 
                        0.0, 
                        0.0, 
                        0.0, 
                        float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                        "REJECTED", 
                        f"NO_ALREADY_PRICED: {yes_price:.4f}"
                    )
                    return False, f"NO_ALREADY_PRICED: {yes_price:.4f}"

        # ─── Price Approach Direction Gate (Falling Knife Rejection) ───
        # Compare YES price now vs YES price 15 seconds ago to verify the approach direction.
        # This prevents the bot from buying late pullback "falling knives" where momentum has reversed.
        # Exempt the early phase (first 25 seconds of candle start) to prevent opening spread noise from triggering rejections.
        is_opening_phase = False
        if new_closes_at > 0:
            duration_minutes = market.get("window_minutes") or 5
            duration_seconds = duration_minutes * 60
            secs_left = new_closes_at - int(time.time())
            elapsed_seconds = duration_seconds - secs_left
            if 0 <= elapsed_seconds < 25:
                is_opening_phase = True
                logger.info(
                    "falling_knife_skipped_opening_phase",
                    market_id=market_id,
                    elapsed_seconds=elapsed_seconds,
                    message="Bypassing Falling Knife Gate during the initial 25s opening phase of the candle."
                )

        yes_price_15s_ago = None
        if not is_opening_phase and not is_reversal:
            yes_price_15s_ago = await get_historical_yes_price(self.db, market_id, seconds_ago=15)
        
        if yes_price_15s_ago is not None and not is_reversal:
            price_direction = yes_price - yes_price_15s_ago
            local_price_velocity = price_direction  # Store velocity for database telemetry
            
            # For YES entry: reject if YES price fell by 3.0 to 6.0 cents in the last 15s (toxic pullback band)
            # Allows minor noise (fell < 3.0c) and deep reversion wicks (fell >= 6.0c)
            if side == "YES" and -0.060 < price_direction <= -0.030:
                logger.warning(
                    "falling_knife_prevented",
                    market_id=market_id,
                    side=side,
                    yes_now=yes_price,
                    yes_15s_ago=yes_price_15s_ago,
                    direction=round(price_direction, 4),
                    message=f"FALLING KNIFE PREVENTED: YES price is in toxic pullback band (fell {price_direction:.4f} in 15s). Rejecting entry."
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"FALLING_KNIFE_YES_APPROACH_{price_direction:.4f}",
                        failed_checks=["price_approach_direction_gate"],
                        market_data=market
                    )
                # Log signal rejection to db with calculated price velocity
                await self._log_signal(
                    market, 
                    side, 
                    0.0, 
                    0.0, 
                    0.0, 
                    float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                    "REJECTED", 
                    f"FALLING_KNIFE: YES price fell {price_direction:.4f} in 15s (toxic band)",
                    price_velocity=local_price_velocity
                )
                return False, f"Falling Knife: YES price fell {price_direction:.4f} in 15s (toxic band)"
                
            # For NO entry: reject if YES price rose by 3.0 to 6.0 cents in the last 15s (meaning NO price fell by 3.0 to 6.0 cents)
            # Allows minor noise and deep reversion wicks
            elif side == "NO" and 0.030 <= price_direction < 0.060:
                logger.warning(
                    "falling_knife_prevented",
                    market_id=market_id,
                    side=side,
                    yes_now=yes_price,
                    yes_15s_ago=yes_price_15s_ago,
                    direction=round(price_direction, 4),
                    message=f"FALLING KNIFE PREVENTED: NO price is in toxic pullback band (YES rose {price_direction:.4f} in 15s). Rejecting entry."
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"FALLING_KNIFE_NO_APPROACH_{price_direction:.4f}",
                        failed_checks=["price_approach_direction_gate"],
                        market_data=market
                    )
                # Log signal rejection to db with calculated price velocity
                await self._log_signal(
                    market, 
                    side, 
                    0.0, 
                    0.0, 
                    0.0, 
                    float(new_closes_at - int(time.time())) if new_closes_at > 0 else 0.0, 
                    "REJECTED", 
                    f"FALLING_KNIFE: NO price fell {-price_direction:.4f} in 15s (toxic band)",
                    price_velocity=local_price_velocity
                )
                return False, f"Falling Knife: NO price fell {-price_direction:.4f} in 15s (toxic band)"


        # ─── Step -2: Spread Guard (Max Entry Price) ───
        opposing_price = no_price if side == "YES" else yes_price
        fair_entry_price = max(0.01, 1.0 - opposing_price)
        
        mult = self.config.max_no_ask_multiplier
        if any(asset.strip().upper() in market_question.upper() for asset in self.config.high_vol_assets):
            mult = self.config.high_vol_ask_multiplier
            
        max_entry_price = min(1.0, round(fair_entry_price * mult, 4))
        if not is_reversal and entry_price > max_entry_price:
            logger.info(
                "spread_guard_rejected", 
                market_id=market_id, 
                entry_price=entry_price, 
                opposing_price=opposing_price, 
                message=f"Spread too wide: {side} price ({entry_price}) > max allowed ({max_entry_price})"
            )
            if self.rejection_logger:
                await self.rejection_logger.log_rejection(
                    market_id=market_id,
                    question=market_question,
                    primary_reason=f"SPREAD_GUARD_{side}_PRICE_{entry_price}_GT_{max_entry_price}",
                    failed_checks=["spread_multiplier_guard"],
                    market_data=market
                )
            return False, f"{side} price ({entry_price}) exceeds maximum allowed ({max_entry_price})"

        # ─── Step -1: Global Concurrency & Cooldown Guard ───
        if True:
            now = time.time()
            
            asset_keywords = ["bitcoin", "ethereum", "solana", "dogecoin", "xrp", "bnb", "hyperliquid"]
            current_asset = next((kw for kw in asset_keywords if kw in market_question.lower()), "unknown")

            time_since_last = now - _last_entry_time
            cooldown = self.config.global_cooldown
            
            # HARD LIMIT: Global cooldown is now strict.
            if time_since_last < cooldown:
                wait_needed = int(cooldown - time_since_last)
                logger.info(
                    "global_cooldown_active",
                    market_id=market_id,
                    wait_seconds=wait_needed,
                    message=f"Global cooldown active: skipping signal for {wait_needed}s"
                )
                if self.rejection_logger:
                    await self.rejection_logger.log_rejection(
                        market_id=market_id,
                        question=market_question,
                        primary_reason=f"GLOBAL_COOLDOWN_{wait_needed}S",
                        failed_checks=["global_cooldown"],
                        market_data=market
                    )
                return False, f"Global entry cooldown: {wait_needed}s remaining"

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

            if self.config.target_market_mode == "crypto_short_window" or is_short_window:
                # Respect .env limit
                if len(active_open_positions) >= self.config.max_open_positions:
                    logger.warning(
                        "global_limit_reached",
                        count=len(active_open_positions),
                        max_limit=self.config.max_open_positions,
                        message=f'Hard limit of {self.config.max_open_positions} concurrent positions reached'
                    )
                    if self.rejection_logger:
                        await self.rejection_logger.log_rejection(
                            market_id=market_id,
                            question=market_question,
                            primary_reason=f"HARD_LIMIT_CONCURRENCY_{len(active_open_positions)}",
                            failed_checks=["global_concurrency_limit"],
                            market_data=market
                        )
                    return False, f"Global concurrency limit ({self.config.max_open_positions}) reached"

            # ─── Step 0.5: Similarity Guard (Cluster Protection) ───
            # In crypto_short_window mode, we allow different time windows for the same asset,
            # BUT we enforce a strict 1-trade-per-asset cap to prevent cluster doom streaks.
            new_core = get_core_context(market_question)
            
            if self.config.target_market_mode == "crypto_short_window" or is_short_window:
                for pos in active_open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    existing_q = existing_m.get("question", "").lower()
                    existing_asset = next((kw for kw in asset_keywords if kw in existing_q), "unknown_old")
                    
                    # If the core asset is the same, log warning but do not reject (disabled per user request)
                    if current_asset != "unknown" and current_asset == existing_asset:
                        logger.info(
                            "asset_cap_bypassed",
                            market_id=market_id,
                            asset=current_asset,
                            message=f"Cluster risk: already holding an open position for '{current_asset}' (guard disabled)"
                        )
            else:
                # Original logic for non-crypto mode
                for pos in active_open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    
                    existing_core = get_core_context(existing_m["question"])
                    
                    if new_core == existing_core:
                        existing_closes_at = existing_m.get("closes_at") or 0
                        
                        if new_closes_at < existing_closes_at and new_closes_at > 0:
                            logger.info(
                                "similarity_swap",
                                new_id=market_id,
                                old_id=pos["market_id"],
                                context=new_core,
                                message=f"Shorter duration found for '{new_core}'. Closing old and swapping."
                            )
                            await self.position_mgr.manual_close_position(pos["position_id"])
                            break
                        else:
                            logger.warning(
                                "similarity_rejected",
                                market_id=market_id,
                                context=new_core,
                                message=f"Clustered market rejected: already holding shorter duration for '{new_core}'"
                            )
                            return False, f"Correlation risk: duplicate context '{new_core}'"

            # ─── Step 1: Run all 9 validation checks ───
            validation = await self.validator.validate(market, target_token_id)
            would_reject_reasons = []

            if is_reversal:
                # Bypass validation failures except check_4 (duplicate)
                check_4_passed = validation.checks.get("4_duplicate", True)
                if check_4_passed:
                    logger.info("reversal_streak_bypassing_validation", market_id=market_id, streak_depth=streak_depth)
                    validation.passed = True
                    validation.reasons = []

            # Compute CVD and Order Book Imbalance pre-checks
            cvd_60s = 0.0
            cvd_5m = 0.0
            if self.binance_ws and self.binance_ws.is_connected:
                cvd_60s, cvd_5m = self.binance_ws.get_cvd_metrics(symbol)
                
            book_imbalance = 0.0
            try:
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

            # ─── CVD Confirmation Gate (B1) ───
            if not is_reversal and self.binance_ws and self.binance_ws.is_connected:
                # 🛡️ CVD Timeframe-Optimized Gate:
                # For 5-minute markets, the CVD guard is disabled to prevent blocking highly profitable blow-off tops / mean reversions.
                # For 15-minute markets, the CVD guard uses a calibrated threshold of $100,000.0 to specifically block high-toxicity institutional trends.
                if window == 15 and side == "NO" and cvd_60s > 100000.0:
                    logger.warning("cvd_gate_rejected", symbol=symbol, side=side, cvd_60s=cvd_60s, window=window)
                    await self._log_signal(market, side, 0.0, book_imbalance, cvd_60s, secs_left, "REJECTED", f"CVD gate: 15m NO positive > 100k ({cvd_60s:.1f})")
                    return False, f"CVD gate: 15m NO rejected due to positive CVD > 100k ({cvd_60s:.1f})"

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
                if yes_bid and yes_ask and yes_ask > 0:
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

            if not is_reversal and ev_ratio < scaled_ev_threshold:
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
            # ─── Step 3: Slippage guard (DISABLED per User Request) ───
            if False: # validation.available_depth > 0:
                required_depth = slippage_depth_required(self.config.position_size_usdc, self.config.slippage_depth_multiplier)
                if validation.available_depth < required_depth:
                    logger.warning(
                        "insufficient_depth",
                        market_id=market_id,
                        available_depth=round(validation.available_depth, 2),
                        required_depth=round(required_depth, 2),
                        message=f"Depth too thin: {validation.available_depth:.2f} < {required_depth:.2f}",
                    )
                    await log_event(
                        self.db,
                        event_type="SIGNAL_REJECTED",
                        bot_version=self.config.bot_version,
                        market_id=market_id,
                        payload={
                            "reason": "INSUFFICIENT_DEPTH",
                            "available": validation.available_depth,
                            "required": required_depth,
                        },
                    )
                    return False, f"Insufficient depth: {validation.available_depth:.2f}"

            # ─── Step 4: Quantitative Sizing & API Minimums ───
            # Restored the highly successful May 26 Symmetric Dynamic Sizing Engine.
            # Uses live Quarter-Kelly sizer, timeframes/correlation skews, and symmetric streak-proportional ceilings.
            direction = "UP" if side == "NO" else "DOWN"
            
            stats = await get_rolling_win_rate(self.db, symbol, str(window), direction)
            n_signals_resolved = stats.get("n_signals_resolved", 0)
            rolling_win_rate = stats.get("rolling_win_rate", 0.5)
            
            # Query preceding positions of the same (symbol, window) to compute cumulative loss of current reversal streak (starting at streak 3)
            cumulative_loss = 0.0
            prev_loss = 0.0
            streak_depth = market.get("streak_depth", 2)
            try:
                table = "paper_positions" if self.config.paper_trade else "positions"
                needed_trades = streak_depth - 3
                if needed_trades > 0:
                    cursor = await self.db.execute(
                        f"""
                        SELECT p.realised_pnl, p.size_usdc, p.opened_at, p.window_minutes, MAX(s.streak_depth) as streak_depth 
                        FROM {table} p
                        JOIN signals s ON p.market_id = s.market_id
                        WHERE p.symbol = ? AND p.window_minutes = ? AND p.order_status IN ('FILLED', 'OPEN') 
                        GROUP BY p.position_id
                        ORDER BY p.opened_at DESC LIMIT ?
                        """,
                        (symbol, window, needed_trades),
                    )
                    rows = await cursor.fetchall()
                    now_ts = int(time.time())
                    for i, row in enumerate(rows):
                        pnl, size_usdc, opened_at, window_min, row_depth = row
                        expected_depth = streak_depth - 1 - i
                        
                        # Only sum if the retrieved trade's streak_depth matches the expected descending sequence
                        if row_depth == expected_depth:
                            if pnl is not None:
                                if pnl < 0.0:
                                    cumulative_loss += abs(pnl)
                                    if i == 0:
                                        prev_loss = abs(pnl)
                                else:
                                    # Hit a win inside the sequence, stop tracing
                                    break
                            else:
                                # Expired unresolved position (bypass 1-second settlement delay)
                                # Calculate candle start by rounding down opened_at to the window boundary
                                candle_dur_secs = (window_min or 5) * 60
                                candle_start = (opened_at // candle_dur_secs) * candle_dur_secs
                                expected_close = candle_start + candle_dur_secs
                                if now_ts >= expected_close - 5:
                                    cumulative_loss += size_usdc
                                    if i == 0:
                                        prev_loss = size_usdc
                                    logger.info("sizer_unresolved_expired_position_treated_as_loss", symbol=symbol, window=window, size=size_usdc)
                                else:
                                    # Still active / unexpired, stop tracing
                                    break
                        else:
                            # Not part of the same continuous streak sequence, stop tracing
                            break
                if cumulative_loss > 0.0:
                    logger.info("sizer_reversal_streak_losses_detected", symbol=symbol, window=window, streak_depth=streak_depth, cumulative_loss=cumulative_loss, prev_loss=prev_loss)
            except Exception as e:
                logger.error("failed_calculating_cumulative_loss", symbol=symbol, window=window, streak_depth=streak_depth, error=str(e))

            # Dynamic Capital Sizing by Streak Depth (Martingale Reversal scale-in)
            streak_depth = market.get("streak_depth", 2)
            if streak_depth == 2:
                # Mild edge: play defensively (0.5x scaling factor)
                streak_multiplier = 0.5
            elif streak_depth == 3:
                # Reversal fade start: 1.0x scaling
                streak_multiplier = 1.0
            elif streak_depth == 4:
                # Extended reversal fade: 1.8x scaling
                streak_multiplier = 1.8
            elif streak_depth == 5:
                # Extreme reversal fade: 3.0x scaling
                streak_multiplier = 3.0
            elif streak_depth >= 6:
                # Streak 6+: Flat scaling to strictly focus on loss-recovery override
                streak_multiplier = 1.0
            else:
                streak_multiplier = 1.0

            if is_reversal:
                # Reversal/Martingale setup: bypass Kelly estimation to avoid collapse on low win rate regimes
                base_size = self.config.position_size_usdc * streak_multiplier
                logger.info("sizing_reversal_kelly_bypassed", symbol=symbol, window=window, streak_depth=streak_depth, multiplier=streak_multiplier, size=base_size)
            elif n_signals_resolved < 15:
                # Default to 50% of the config base size to avoid parameter estimation error, scaled by streak
                base_size = self.config.position_size_usdc * 0.5 * streak_multiplier
                logger.info("sizing_kelly_low_opportunity_count", symbol=symbol, window=window, count=n_signals_resolved, streak_depth=streak_depth, multiplier=streak_multiplier, size=base_size)
            else:
                # Re-compute Kelly fraction live at trade time using live price and simulated 2% friction fees
                # Enforce a strict 80.00% cap on the rolling win rate to prevent over-leveraging on early streaks
                p = min(0.80, rolling_win_rate)
                # Net odds b = (0.98 / entry_price) - 1.0. Accounts for simulated 2% transaction friction.
                b = (0.98 / entry_price) - 1.0 if entry_price > 0.0 else 0.0
                
                # Compute live Half-Kelly: max(0.0, (p - (1 - p)/b) * 0.5)
                live_half_kelly = max(0.0, (p - (1.0 - p) / b) * 0.5) if b > 0.0 else 0.0
                
                # Apply another 0.5x global defensive multiplier on top of live Half-Kelly,
                # resulting in a highly robust, drawdown-cushioned Quarter-Kelly (Full Kelly * 0.5 * 0.5),
                # scaled dynamically by the streak_multiplier
                base_size = live_half_kelly * current_bankroll * 0.5 * streak_multiplier
                logger.info(
                    "sizing_kelly_calculated_live",
                    symbol=symbol,
                    window=window,
                    rolling_win_rate=p,
                    live_entry_price=entry_price,
                    live_odds_b=b,
                    live_half_kelly=live_half_kelly,
                    streak_depth=streak_depth,
                    streak_multiplier=streak_multiplier,
                    effective_sizing_fraction="Quarter-Kelly * streak_multiplier",
                    bankroll=current_bankroll,
                    size=base_size
                )
                
            # Symmetric Sizing Ceilings based strictly on Streak Depth (May 26 Baseline)
            # Both YES and NO sides have identical ceilings: config.position_size_usdc * streak_multiplier
            max_size_allowed = self.config.position_size_usdc * streak_multiplier

            sizing = min(max_size_allowed, base_size)
            
            # Timeframe Sizing Skew (C1: 60/40 Window Skew - Step 4)
            # 5m markets get 1.2x weight, 15m markets get 0.8x weight
            window_skew = 1.2 if window == 5 else 0.8
            sizing = sizing * window_skew
            logger.info("sizing_timeframe_skew_applied", symbol=symbol, window=window, skew=window_skew, new_size=sizing)
            
            # Floor of $1.00, Ceiling strictly capped by max_size_allowed * window_skew
            max_cap = max_size_allowed * window_skew
            sizing = max(1.0, min(max_cap, sizing))
            
            # A3: Correlated Asset Penalty (Strict Timeframe Window Alignment)
            has_correlated_same_direction = False
            corr_symbol = "ETH" if symbol == "BTC" else ("BTC" if symbol == "ETH" else None)
            if corr_symbol:
                for pos in active_open_positions:
                    existing_m = await get_market(self.db, pos["market_id"])
                    if not existing_m: continue
                    existing_q = existing_m.get("question", "").lower()
                    existing_symbol = "ETH" if "eth" in existing_q or "ethereum" in existing_q else ("BTC" if "btc" in existing_q or "bitcoin" in existing_q else None)
                    if existing_symbol == corr_symbol:
                        # Extract existing timeframe duration from slug/question
                        existing_slug = existing_m.get("slug", "").lower()
                        existing_window = 15 if ("-15m-" in existing_slug or "15-minute" in existing_q) else 5
                        
                        # Only apply correlation penalty if timeframe window sizes match
                        if existing_window == window:
                            existing_side = pos.get("side", "NO")
                            if existing_side == side:
                                has_correlated_same_direction = True
                                break
            
            if is_reversal:
                corr_mult = 1.0
                if has_correlated_same_direction:
                    logger.info("sizing_correlated_penalty_paused_on_reversal_streak", symbol=symbol, streak_depth=streak_depth)
            else:
                corr_mult = calculate_correlated_penalty_multiplier(has_correlated_same_direction)
                if has_correlated_same_direction:
                    logger.info("sizing_correlated_penalty_applied", symbol=symbol, penalty=0.5)
            sizing = sizing * corr_mult
            
            # Linear Time Decay Multiplier
            if new_closes_at > 0:
                secs_left = new_closes_at - int(time.time())
                total_window = window * 60
                decay_mult = get_linear_time_decay_multiplier(secs_left, total_window, min_mult=0.5)
                logger.info("sizing_time_decay_applied", symbol=symbol, time_left=secs_left, total=total_window, multiplier=decay_mult)
                sizing = sizing * decay_mult
                
            # Keep final sizing clean
            sizing = round(sizing, 2)
            logger.info("sizing_final_computed", symbol=symbol, window=window, final_size=sizing)
            
            raw_shares = sizing / entry_price
            shares = float(int(raw_shares))
            current_size_usdc = shares * entry_price

            # Refined Streak-Bound Reversal Cumulative Loss-Recovery Sizer Boost
            if streak_depth >= 3 and cumulative_loss > 0.0 and entry_price < 1.0:
                # Target Profit Calibration:
                # Streak <= 5 -> recover sequence losses + make 1x base position size profit
                # Streak >= 6 -> strict break-even (Target Profit = cumulative_loss)
                if streak_depth <= 5:
                    target_profit = cumulative_loss + self.config.position_size_usdc
                    profit_regime = "RECOVERY_PLUS_WIN"
                else:
                    target_profit = cumulative_loss
                    profit_regime = "STRICT_BREAK_EVEN"

                standard_projected_profit = shares * (1.0 - entry_price)
                if standard_projected_profit < target_profit:
                    import math
                    boosted_shares = float(math.ceil(target_profit / (1.0 - entry_price)))
                    boosted_size = boosted_shares * entry_price
                    
                    logger.info(
                        "sizer_loss_recovery_boost_triggered",
                        symbol=symbol,
                        window=window,
                        streak_depth=streak_depth,
                        profit_regime=profit_regime,
                        prev_loss=prev_loss,
                        cumulative_loss=cumulative_loss,
                        target_profit=target_profit,
                        standard_profit=standard_projected_profit,
                        boosted_shares=boosted_shares,
                        boosted_size=boosted_size,
                    )
                    shares = boosted_shares
                    current_size_usdc = boosted_size

            # Enforce Polymarket CLOB minimum order sizes
            order_min_size = market.get("order_min_size", 5.0)

            if shares < order_min_size:
                if (self.config.target_market_mode == "crypto_short_window" or is_short_window) and self.config.paper_trade:
                    # R&D paper mode: clamp to minimum and UPDATE REAL COST
                    shares = order_min_size
                    current_size_usdc = shares * entry_price
                    logger.info(
                        "shares_clamped_to_minimum",
                        market_id=market_id,
                        clamped_to=order_min_size,
                        requested_size=self.config.position_size_usdc,
                        actual_cost=round(current_size_usdc, 3),
                        message=f"Bumping to {order_min_size} shares to meet Polymarket API minimum (Cost: ${current_size_usdc:.2f})",
                    )
                else:
                    logger.warning(
                        "insufficient_shares",
                        market_id=market_id,
                        calculated_shares=round(shares, 2),
                        required_minimum=order_min_size,
                        message=f"Calculated shares ({shares:.2f}) is below API minimum ({order_min_size})",
                    )
                    return False, f"Shares calculated (< {order_min_size}) violates Polymarket API minimum size"
            # ─── Step 5: Execute via Position Manager ───
            if True:  # Removed nested lock wrapper (now serialized at loop level in main.py)
                if time.time() - _last_entry_time < self.config.global_cooldown:
                    return False, f"Global Cooldown caught by lock"

                # Calculate spread at entry
                spread = 0.0
                yes_tid = market.get("yes_token_id")
                if yes_tid and self.position_mgr.ws:
                    yes_bid = self.position_mgr.ws.bid_cache.get(yes_tid)
                    yes_ask = self.position_mgr.ws.ask_cache.get(yes_tid)
                    if yes_bid is not None and yes_ask is not None:
                        spread = float(yes_ask) - float(yes_bid)

                # Calculate market age percentage
                market_age_at_entry_pct = 0.0
                if new_closes_at > 0:
                    duration = market.get("window_minutes") or 5
                    total_window_secs = duration * 60
                    elapsed_secs = total_window_secs - secs_left
                    market_age_at_entry_pct = max(0.0, min(1.0, elapsed_secs / total_window_secs))

                success = await self.position_mgr.open_position(
                    market_id=market_id,
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=current_size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=stats.get("rolling_win_rate", 0.5) if 'stats' in locals() else 0.5,
                    kelly_fraction_used=kelly_fraction if 'kelly_fraction' in locals() else 0.0,
                    bid_ask_spread_at_entry=spread,
                    book_imbalance_at_entry=book_imbalance if 'book_imbalance' in locals() else 0.0,
                    cvd_60s_at_entry=cvd_60s if 'cvd_60s' in locals() else 0.0,
                    correlated_position_open=1 if (has_correlated_same_direction if 'has_correlated_same_direction' in locals() else False) else 0,
                    seconds_to_close_at_entry=secs_left,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=current_bankroll if 'current_bankroll' in locals() else 100.0,
                    streak_depth=market.get("streak_depth", 2),
                )

                if success:
                    _last_entry_time = time.time()
                    if self.scanner:
                        closes_at = market.get("closes_at") or 0
                        if closes_at:
                            symbol, window = self.scanner._get_market_key_info(market)
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
                    await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "ACCEPTED", reason=reason_str, price_velocity=local_price_velocity)
                    return True, "Order placed successfully"
                else:
                    await self._log_signal(market, side, ev_ratio, book_imbalance, cvd_60s, secs_left, "REJECTED", "Position manager open_position failed", price_velocity=local_price_velocity)
                    return False, "Position manager failed to execute order"

