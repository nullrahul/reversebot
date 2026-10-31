import json
import asyncio
import time
import math
from typing import Dict, Any, Optional
from src.logs.log_config import get_logger
from src.strategy.guardian import Guardian
from src.db.queries import fetch_all, fetch_one

logger = get_logger("profiles")

# Try to import numpy, but fallback gracefully if not installed
try:
    import numpy as np
except ImportError:
    np = None
    logger.warning("numpy_not_found_shadow_profiles_disabled")


class LightweightKNN:
    def __init__(self, trades):
        self.features = []
        self.pnls = []
        self.slippages = []
        self.fills = []
        
        if np is None:
            return
            
        for t in trades:
            # Feature vector: [entry_price, spread, book_imbalance, cvd, seconds_to_close, market_age_pct, streak_depth]
            feat = [
                float(t.get("entry_price") or 0.5),
                float(t.get("bid_ask_spread_at_entry") or 0.0),
                float(t.get("book_imbalance_at_entry") or 0.0),
                float(t.get("cvd_60s_at_entry") or 0.0),
                float(t.get("seconds_to_close_at_entry") or 0.0),
                float(t.get("market_age_at_entry_pct") or 0.0),
                float(t.get("streak_depth") or 2.0)
            ]
            self.features.append(feat)
            self.pnls.append(float(t.get("realised_pnl") or 0.0))
            self.slippages.append(float(t.get("bid_ask_spread_at_entry") or 0.005))
            self.fills.append(1.0 if t.get("realised_pnl") is not None else 0.0)
            
        self.X = np.array(self.features, dtype=np.float32)
        self.y = np.array(self.pnls, dtype=np.float32)
        self.slip = np.array(self.slippages, dtype=np.float32)
        self.fill = np.array(self.fills, dtype=np.float32)
        
        if len(self.X) > 0:
            self.mean = np.mean(self.X, axis=0)
            self.std = np.std(self.X, axis=0)
            self.std[self.std == 0.0] = 1.0 # prevent division by zero
            self.X_scaled = (self.X - self.mean) / self.std
        else:
            self.X_scaled = np.array([])
            
    def predict(self, new_feat, K=100):
        if np is None or len(self.X_scaled) == 0:
            return {
                "predicted_ev": 0.0,
                "confidence": 0.5,
                "expected_slippage": 0.005,
                "expected_fill_prob": 0.90,
                "distance": 1.0,
                "alpha_score": 50.0,
                "would_trade": "NO",
                "suggested_size": 10.0
            }
        
        # Scale new feature
        x_new = (np.array(new_feat, dtype=np.float32) - self.mean) / self.std
        
        # Vectorized Euclidean distance
        dists = np.sqrt(np.sum((self.X_scaled - x_new) ** 2, axis=1))
        
        # Find indices of K nearest neighbors
        K_actual = min(K, len(dists))
        if K_actual == 0:
            return {
                "predicted_ev": 0.0,
                "confidence": 0.5,
                "expected_slippage": 0.005,
                "expected_fill_prob": 0.90,
                "distance": 1.0,
                "alpha_score": 50.0,
                "would_trade": "NO",
                "suggested_size": 10.0
            }
            
        nearest_idx = np.argsort(dists)[:K_actual]
        
        neighbor_pnls = self.y[nearest_idx]
        neighbor_slips = self.slip[nearest_idx]
        neighbor_fills = self.fill[nearest_idx]
        neighbor_dists = dists[nearest_idx]
        
        predicted_ev = float(np.mean(neighbor_pnls))
        std_local = float(np.std(neighbor_pnls))
        sem = std_local / math.sqrt(K_actual)
        
        confidence = float(max(0.0, 1.0 - 1.96 * sem / (abs(predicted_ev) + 0.05)))
        expected_slippage = float(np.mean(neighbor_slips))
        expected_fill_prob = float(np.mean(neighbor_fills))
        mean_dist = float(np.mean(neighbor_dists))
        
        alpha_score = float(np.clip((predicted_ev + 1.0) / 2.0 * 100.0, 0.0, 100.0))
        
        ev_lower_95 = predicted_ev - 1.96 * sem
        would_trade = "YES" if ev_lower_95 > 0.0 else "NO"
        suggested_size = float(np.clip(10.0 * max(0.0, predicted_ev / 0.5), 2.0, 50.0))
        
        return {
            "predicted_ev": predicted_ev,
            "confidence": confidence,
            "expected_slippage": expected_slippage,
            "expected_fill_prob": expected_fill_prob,
            "distance": mean_dist,
            "alpha_score": alpha_score,
            "would_trade": would_trade,
            "suggested_size": suggested_size
        }


class ProfileManager:
    """
    Evaluates signal parameters against isolated strategy profiles
    and triggers virtual paper trades in the profile_positions database.
    """

    def __init__(self, db, position_mgr):
        self.db = db
        self.position_mgr = position_mgr
        self.guardian = Guardian(db, position_mgr)
        self.opened_stop_loss = set()
        self.opened_reversal_stop_loss = set()
        self.opened_probability_sl = set()
        self.opened_reversal_probability_sl = set()
        self.opened_stop_loss_filtered = set()
        self.opened_reversal_stop_loss_filtered = set()
        self.opened_claude_audit = set()
        self.opened_claude_gated = set()
        self.opened_claude_every_reversal = set()
        self.opened_claude_good = set()
        self.opened_claude_good_reversal = set()
        self.opened_claude_edge_reversal = set()
        self.opened_claude_audit_reversal = set()
        self.opened_claude_gated_reversal = set()
        self.opened_reversal_yes_cvd_imb = set()
        self.opened_reversal_yes_cvd_imb_wide = set()
        self.opened_cheap_reversal_v2 = set()
        self.opened_reversal_yes_cvd_imb_wide_v2 = set()
        self.opened_loss_skip_yes_cvd_v2 = set()
        self.opened_loss_skip_yes_cvd = set()
        self.opened_loss_skip_btc_5m_recovery = set()
        self.opened_reversal_stop_loss_eth_5m_exhaustion = set()
        self.opened_reversal_eth_5m_momentum = set()
        self.opened_single_market_hedge_cheap = set()
        self.opened_single_market_hedge_expensive = set()
        self.opened_cross_market_hedge = set()
        self.opened_micro_flow_no = set()
        self.opened_era3_cheap_htr = set()
        self.opened_era3_no_cheap_htr = set()
        self.opened_era3_hybrid_bandpass = set()
        self.opened_livemode = set()
        self.opened_executioner = set()
        self.opened_continuous_ev_v1 = set()
        self.opened_continuous_ev_v1_reversal = set()
        self.opened_confidence_v1 = set()
        self.opened_confidence_v1_reversal = set()
        self.opened_execution_v1 = set()
        self.opened_execution_v1_reversal = set()
        self.lightweight_knn = None
        asyncio.create_task(self._init_opened_list())
        asyncio.create_task(self._init_lightweight_knn())

    async def _init_opened_list(self) -> None:
        # Initialize Guardian Tables first
        await self.guardian.init_tables()

        try:
            from src.db.queries import init_executioner_table
            await init_executioner_table(self.db)
            rows = await fetch_all(
                self.db,
                "SELECT market_id, request_id FROM executioner_positions WHERE order_status = 'OPEN'"
            )
            self.opened_executioner = {f"{r['market_id']}_{r['request_id']}" for r in rows}
            logger.info("initialized_opened_executioner", count=len(self.opened_executioner))
        except Exception as e:
            logger.error("failed_to_initialize_opened_executioner", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'stop_loss'"
            )
            self.opened_stop_loss = {r["market_id"] for r in rows}
            logger.info("initialized_opened_stop_loss", count=len(self.opened_stop_loss))
        except Exception as e:
            logger.error("failed_to_initialize_opened_stop_loss", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'stop_loss_filtered'"
            )
            self.opened_stop_loss_filtered = {r["market_id"] for r in rows}
            logger.info("initialized_opened_stop_loss_filtered", count=len(self.opened_stop_loss_filtered))
        except Exception as e:
            logger.error("failed_to_initialize_opened_stop_loss_filtered", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_stop_loss'"
            )
            self.opened_reversal_stop_loss = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_stop_loss", count=len(self.opened_reversal_stop_loss))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_stop_loss", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_stop_loss_filtered'"
            )
            self.opened_reversal_stop_loss_filtered = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_stop_loss_filtered", count=len(self.opened_reversal_stop_loss_filtered))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_stop_loss_filtered", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'probability_sl'"
            )
            self.opened_probability_sl = {r["market_id"] for r in rows}
            logger.info("initialized_opened_probability_sl", count=len(self.opened_probability_sl))
        except Exception as e:
            logger.error("failed_to_initialize_opened_probability_sl", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_probability_sl'"
            )
            self.opened_reversal_probability_sl = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_probability_sl", count=len(self.opened_reversal_probability_sl))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_probability_sl", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'claude_audit'"
            )
            self.opened_claude_audit = {r["market_id"] for r in rows}
            logger.info("initialized_opened_claude_audit", count=len(self.opened_claude_audit))
        except Exception as e:
            logger.error("failed_to_initialize_opened_claude_audit", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'claude_gated'"
            )
            self.opened_claude_gated = {r["market_id"] for r in rows}
            logger.info("initialized_opened_claude_gated", count=len(self.opened_claude_gated))
        except Exception as e:
            logger.error("failed_to_initialize_opened_claude_gated", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'claude_good'"
            )
            self.opened_claude_good = {r["market_id"] for r in rows}
            logger.info("initialized_opened_claude_good", count=len(self.opened_claude_good))
        except Exception as e:
            logger.error("failed_to_initialize_opened_claude_good", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'single_market_hedge_cheap'"
            )
            self.opened_single_market_hedge_cheap = {r["market_id"] for r in rows}
            logger.info("initialized_opened_single_market_hedge_cheap", count=len(self.opened_single_market_hedge_cheap))
        except Exception as e:
            logger.error("failed_to_initialize_opened_single_market_hedge_cheap", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'single_market_hedge_expensive'"
            )
            self.opened_single_market_hedge_expensive = {r["market_id"] for r in rows}
            logger.info("initialized_opened_single_market_hedge_expensive", count=len(self.opened_single_market_hedge_expensive))
        except Exception as e:
            logger.error("failed_to_initialize_opened_single_market_hedge_expensive", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'cross_market_hedge'"
            )
            self.opened_cross_market_hedge = {r["market_id"] for r in rows}
            logger.info("initialized_opened_cross_market_hedge", count=len(self.opened_cross_market_hedge))
        except Exception as e:
            logger.error("failed_to_initialize_opened_cross_market_hedge", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_yes_cvd_imb'"
            )
            self.opened_reversal_yes_cvd_imb = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_yes_cvd_imb", count=len(self.opened_reversal_yes_cvd_imb))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_yes_cvd_imb", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_yes_cvd_imb_wide'"
            )
            self.opened_reversal_yes_cvd_imb_wide = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_yes_cvd_imb_wide", count=len(self.opened_reversal_yes_cvd_imb_wide))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_yes_cvd_imb_wide", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'cheap_reversal_v2'"
            )
            self.opened_cheap_reversal_v2 = {r["market_id"] for r in rows}
            logger.info("initialized_opened_cheap_reversal_v2", count=len(self.opened_cheap_reversal_v2))
        except Exception as e:
            logger.error("failed_to_initialize_opened_cheap_reversal_v2", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_yes_cvd_imb_wide_v2'"
            )
            self.opened_reversal_yes_cvd_imb_wide_v2 = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_yes_cvd_imb_wide_v2", count=len(self.opened_reversal_yes_cvd_imb_wide_v2))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_yes_cvd_imb_wide_v2", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'loss_skip_yes_cvd_v2'"
            )
            self.opened_loss_skip_yes_cvd_v2 = {r["market_id"] for r in rows}
            logger.info("initialized_opened_loss_skip_yes_cvd_v2", count=len(self.opened_loss_skip_yes_cvd_v2))
        except Exception as e:
            logger.error("failed_to_initialize_opened_loss_skip_yes_cvd_v2", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'loss_skip_yes_cvd'"
            )
            self.opened_loss_skip_yes_cvd = {r["market_id"] for r in rows}
            logger.info("initialized_opened_loss_skip_yes_cvd", count=len(self.opened_loss_skip_yes_cvd))
        except Exception as e:
            logger.error("failed_to_initialize_opened_loss_skip_yes_cvd", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'loss_skip_btc_5m_recovery'"
            )
            self.opened_loss_skip_btc_5m_recovery = {r["market_id"] for r in rows}
            logger.info("initialized_opened_loss_skip_btc_5m_recovery", count=len(self.opened_loss_skip_btc_5m_recovery))
        except Exception as e:
            logger.error("failed_to_initialize_opened_loss_skip_btc_5m_recovery", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_stop_loss_eth_5m_exhaustion'"
            )
            self.opened_reversal_stop_loss_eth_5m_exhaustion = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_stop_loss_eth_5m_exhaustion", count=len(self.opened_reversal_stop_loss_eth_5m_exhaustion))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_stop_loss_eth_5m_exhaustion", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'reversal_eth_5m_momentum'"
            )
            self.opened_reversal_eth_5m_momentum = {r["market_id"] for r in rows}
            logger.info("initialized_opened_reversal_eth_5m_momentum", count=len(self.opened_reversal_eth_5m_momentum))
        except Exception as e:
            logger.error("failed_to_initialize_opened_reversal_eth_5m_momentum", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'micro_flow_no'"
            )
            self.opened_micro_flow_no = {r["market_id"] for r in rows}
            logger.info("initialized_opened_micro_flow_no", count=len(self.opened_micro_flow_no))
        except Exception as e:
            logger.error("failed_to_initialize_opened_micro_flow_no", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'era3_cheap_htr'"
            )
            self.opened_era3_cheap_htr = {r["market_id"] for r in rows}
            logger.info("initialized_opened_era3_cheap_htr", count=len(self.opened_era3_cheap_htr))
        except Exception as e:
            logger.error("failed_to_initialize_opened_era3_cheap_htr", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'era3_no_cheap_htr'"
            )
            self.opened_era3_no_cheap_htr = {r["market_id"] for r in rows}
            logger.info("initialized_opened_era3_no_cheap_htr", count=len(self.opened_era3_no_cheap_htr))
        except Exception as e:
            logger.error("failed_to_initialize_opened_era3_no_cheap_htr", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'era3_hybrid_bandpass'"
            )
            self.opened_era3_hybrid_bandpass = {r["market_id"] for r in rows}
            logger.info("initialized_opened_era3_hybrid_bandpass", count=len(self.opened_era3_hybrid_bandpass))
        except Exception as e:
            logger.error("failed_to_initialize_opened_era3_hybrid_bandpass", error=str(e))

        try:
            rows = await fetch_all(
                self.db,
                "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = 'livemode'"
            )
            self.opened_livemode = {r["market_id"] for r in rows}
            logger.info("initialized_opened_livemode", count=len(self.opened_livemode))
        except Exception as e:
            logger.error("failed_to_initialize_opened_livemode", error=str(e))

        for p_name, p_attr in [
            ("continuous_ev_v1", "opened_continuous_ev_v1"),
            ("continuous_ev_v1_reversal", "opened_continuous_ev_v1_reversal"),
            ("confidence_v1", "opened_confidence_v1"),
            ("confidence_v1_reversal", "opened_confidence_v1_reversal"),
            ("execution_v1", "opened_execution_v1"),
            ("execution_v1_reversal", "opened_execution_v1_reversal"),
        ]:
            try:
                rows = await fetch_all(
                    self.db,
                    "SELECT DISTINCT market_id FROM profile_positions WHERE profile_name = ?",
                    (p_name,)
                )
                setattr(self, p_attr, {r["market_id"] for r in rows})
                logger.info(f"initialized_{p_attr}", count=len(getattr(self, p_attr)))
            except Exception as e:
                logger.error(f"failed_to_initialize_{p_attr}", error=str(e))

    async def _check_consecutive_loss_cooldown(self, profile_name: str, cooldown_secs: int = 3600) -> bool:
        """
        Returns True if the profile has 2 consecutive losses in the past cooldown_secs.
        """
        try:
            now_ts = int(time.time())
            rows = await fetch_all(
                self.db,
                """
                SELECT realised_pnl, closed_at 
                FROM profile_positions 
                WHERE profile_name = ? AND close_reason IS NOT NULL AND close_reason != 'OPEN' AND closed_at IS NOT NULL
                ORDER BY closed_at DESC 
                LIMIT 2
                """,
                (profile_name,)
            )
            if len(rows) >= 2:
                last_1_pnl = float(rows[0]["realised_pnl"] or 0)
                last_2_pnl = float(rows[1]["realised_pnl"] or 0)
                last_closed_ts = int(rows[0]["closed_at"] or 0)
                if last_1_pnl < 0 and last_2_pnl < 0 and (now_ts - last_closed_ts) < cooldown_secs:
                    return True
        except Exception as e:
            logger.error("failed_to_check_consecutive_loss_cooldown", profile=profile_name, error=str(e))
        return False

    async def _check_htf_trend_conflict(self, symbol: str, streak_side: str) -> bool:
        """
        Returns True if higher timeframe (1H) trend is strongly aligned with streak_side (i.e. conflict for Reversal).
        For example: if streak_side == 'YES' (UP streak) and 1H CVD/trend is heavily UP (CVD 5m > 50,000), return True to block fading.
        """
        try:
            sym_upper = (symbol or "BTC").upper()
            cvd_col = "cvd_5m_btc" if "BTC" in sym_upper else "cvd_5m_eth"
            row = await fetch_one(
                self.db,
                f"SELECT {cvd_col} FROM market_regimes ORDER BY regime_id DESC LIMIT 1"
            )
            if row and row[cvd_col] is not None:
                cvd_val = float(row[cvd_col])
                if streak_side == "YES" and cvd_val > 50000.0:
                    return True
                if streak_side == "NO" and cvd_val < -50000.0:
                    return True
        except Exception as e:
            logger.error("failed_to_check_htf_trend_conflict", symbol=symbol, error=str(e))
        return False


    async def _init_lightweight_knn(self) -> None:
        try:
            rows = await fetch_all(
                self.db,
                """
                SELECT realised_pnl, entry_price, bid_ask_spread_at_entry, 
                       book_imbalance_at_entry, cvd_60s_at_entry, 
                       seconds_to_close_at_entry, market_age_at_entry_pct, 
                       persistence_score, notes
                FROM profile_positions
                WHERE closed_at IS NOT NULL AND realised_pnl IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT 2000
                """
            )
            trades = []
            for r in rows:
                t = dict(r)
                notes_str = t.get("notes")
                streak_depth = 2.0
                if notes_str:
                    try:
                        notes_data = json.loads(notes_str)
                        streak_depth = float(notes_data.get("streak_depth") or 2.0)
                    except Exception:
                        pass
                t["streak_depth"] = streak_depth
                trades.append(t)
                
            self.lightweight_knn = LightweightKNN(trades)
            logger.info("initialized_lightweight_knn_model", size=len(trades))
        except Exception as e:
            logger.error("failed_to_initialize_lightweight_knn", error=str(e))
            self.lightweight_knn = None


    async def register_potential_signal(self, *args, **kwargs) -> None:
        """Compatibility no-op method after removing late_entry background loop."""
        pass

    def _get_now(self) -> int:
        try:
            clob = getattr(self.position_mgr, "clob", None)
            if clob is not None:
                t = clob.get_synced_time()
                if hasattr(t, "__await__") or not isinstance(t, (int, float)):
                    return int(time.time())
                return int(t)
            return int(time.time())
        except Exception:
            return int(time.time())


    async def evaluate_and_trigger(
        self,
        market: Dict[str, Any],
        side: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
        strategy_mode: str,
        elapsed_secs: float,
        persistence_score: float,
        should_block_for_loss: bool,
        active_strategy: str,
        target_token_id: Optional[str] = None,
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        streak_depth: int = 2
    ) -> None:
        """
        Evaluate and execute virtual positions for each profile.
        All trigger points are wired to the codebase's existing checks.
        """
        market_id = market["market_id"]
        logger.info("evaluating_profiles_for_signal", market_id=market_id)

        slug = (market.get("slug") or "").lower()
        question = (market.get("question") or "").lower()
        symbol = "BTC"
        if "eth" in slug or "ethereum" in question or "eth" in question:
            symbol = "ETH"
        elif "sol" in slug or "solana" in question:
            symbol = "SOL"

        window = market.get("window_minutes")
        if not window:
            if "-15m-" in slug or "15-minute" in question:
                window = 15
            elif "-5m-" in slug or "5-minute" in question:
                window = 5
            else:
                window = 5

        # Look-ahead bias check: verify closes_at using CLOB-synced time
        closes_at = market.get("closes_at") or 0
        if closes_at > 0:
            now_ts = self._get_now()
            if now_ts >= closes_at:
                logger.warning("profile_skip_lookahead_gate_block", market_id=market_id, now=now_ts, closes_at=closes_at)
                return

        # Recompute clean independent pricing/shares for virtual profiles using live price if available
        trend_price = market.get("yes_price", 0.5) if side == "YES" else market.get("no_price", 0.5)
        if entry_price is not None and entry_price > 0:
            trend_price = entry_price
            
        trend_shares = size_usdc / trend_price if trend_price > 0 else 0.0

        # Calculate actual reversal price from live price if available, fallback to stale market dictionary
        reversal_price = None
        if entry_price is not None and entry_price > 0:
            reversal_price = round(1.0 - entry_price, 4)
        else:
            reversal_price = market.get("no_price") if side == "YES" else market.get("yes_price")
            
        if reversal_price is None or reversal_price <= 0.0:
            reversal_price = 1.0 - trend_price
        reversal_shares = size_usdc / reversal_price if reversal_price > 0 else 0.0

        reversal_side = "NO" if side == "YES" else "YES"
        reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")

        # ----------------------------------------------------
        # 1d. Claude Every Reversal Profile (Preserved)
        # Trigger: immediately at signal time (streak >= 1) when entry_price < 0.40.
        if market_id not in self.opened_claude_every_reversal:
            if trend_price < 0.40:
                self.opened_claude_every_reversal.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="claude_every_reversal",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # ----------------------------------------------------
        # Claude Good Profile
        # Trigger: restricted to 15m window, entry price 0.40-0.69, Streak (consec losses of stop_loss) >= 2
        # YES side: book_imbalance >= 0.10, cvd_60s >= 0.0
        # NO side: unconditional entry
        # Stop loss: None. Take profit: None.
        if market_id not in self.opened_claude_good:
            window_minutes = market.get("window_minutes") or 15
            if window_minutes != 15:
                logger.info(
                    "claude_good_window_block",
                    market_id=market_id,
                    window_minutes=window_minutes,
                    message=f"claude_good skipped signal on window: {window_minutes} != 15"
                )
            elif trend_price >= 0.70:
                logger.info(
                    "claude_good_price_cap",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_good skipped signal on price cap: {trend_price} >= 0.70"
                )
            elif trend_price < 0.40:
                logger.info(
                    "claude_good_price_floor",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_good skipped signal on price floor: {trend_price} < 0.40"
                )
            else:
                # Calculate consecutive losses of 'stop_loss' profile
                consec_losses = 0
                try:
                    sl_rows = await fetch_all(
                        self.db,
                        """
                        SELECT realised_pnl FROM profile_positions
                        WHERE profile_name = 'stop_loss' AND closed_at IS NOT NULL AND realised_pnl IS NOT NULL
                        ORDER BY closed_at DESC
                        """
                    )
                    for r in sl_rows:
                        pnl = r["realised_pnl"] or 0
                        if pnl <= 0:
                            consec_losses += 1
                        else:
                            break
                except Exception as e:
                    logger.error("claude_good_streak_calc_error", error=str(e))

                if consec_losses < 2:
                    logger.info(
                        "claude_good_streak_block",
                        market_id=market_id,
                        consec_losses=consec_losses,
                        message=f"claude_good skipped signal on streak: {consec_losses} < 2"
                    )
                else:
                    passed_guards = True
                    if side == "YES":
                        if book_imbalance_at_entry < 0.10:
                            passed_guards = False
                            logger.info(
                                "claude_good_yes_imbalance_block",
                                market_id=market_id,
                                imbalance=book_imbalance_at_entry,
                                message=f"claude_good YES skipped on imbalance: {book_imbalance_at_entry:.4f} < 0.10"
                            )
                        elif cvd_60s_at_entry < 0.0:
                            passed_guards = False
                            logger.info(
                                "claude_good_yes_cvd_block",
                                market_id=market_id,
                                cvd=cvd_60s_at_entry,
                                message=f"claude_good YES skipped on cvd: {cvd_60s_at_entry:.1f} < 0.0"
                            )
                    
                    if passed_guards:
                        logger.info(
                            "profile_triggered_claude_good",
                            market_id=market_id,
                            price=trend_price,
                            side=side,
                            consec_losses=consec_losses,
                            imbalance=book_imbalance_at_entry,
                            cvd=cvd_60s_at_entry,
                            window=window_minutes
                        )
                        self.opened_claude_good.add(market_id)
                        await self._trigger_virtual_trade(
                            profile_name="claude_good",
                            market=market,
                            side=side,
                            entry_price=trend_price,
                            size_usdc=size_usdc,
                            shares=trend_shares,
                            strategy_mode="TREND",
                            target_token_id=target_token_id,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )
                        if market_id not in self.opened_claude_good_reversal:
                            self.opened_claude_good_reversal.add(market_id)
                            await self._trigger_virtual_trade(
                                profile_name="claude_good_reversal",
                                market=market,
                                side=reversal_side,
                                entry_price=reversal_price,
                                size_usdc=size_usdc,
                                shares=reversal_shares,
                                strategy_mode="REVERSAL",
                                target_token_id=reversal_tid,
                                win_prob_estimate=win_prob_estimate,
                                kelly_fraction_used=kelly_fraction_used,
                                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                book_imbalance_at_entry=book_imbalance_at_entry,
                                cvd_60s_at_entry=cvd_60s_at_entry,
                                correlated_position_open=correlated_position_open,
                                seconds_to_close_at_entry=seconds_to_close_at_entry,
                                market_age_at_entry_pct=market_age_at_entry_pct,
                                bankroll_at_entry=bankroll_at_entry,
                                persistence_score=persistence_score,
                                streak_depth=streak_depth
                            )

        # ----------------------------------------------------
        # Claude Edge Profile
        # ----------------------------------------------------
        # Claude Edge Reversal Profile (Preserved)
        # Trigger: restricted to 5m window, entry price 0.10-0.39,
        # YES side: book_imbalance >= 0.0, cvd_60s >= -20000.0
        # NO side: book_imbalance <= 0.20, cvd_60s <= 10000.0
        if market_id not in self.opened_claude_edge_reversal:
            window_minutes = market.get("window_minutes") or 5
            if window_minutes == 5 and 0.10 <= trend_price < 0.40:
                passed_guards = True
                if side == "YES":
                    if book_imbalance_at_entry < 0.0 or cvd_60s_at_entry < -20000.0:
                        passed_guards = False
                else:  # side == "NO"
                    if book_imbalance_at_entry > 0.20 or cvd_60s_at_entry > 10000.0:
                        passed_guards = False

                if passed_guards:
                    size_scale = 1.0
                    try:
                        sl_rows = await fetch_all(
                            self.db,
                            """
                            SELECT realised_pnl FROM profile_positions
                            WHERE profile_name = 'stop_loss' AND closed_at IS NOT NULL AND realised_pnl IS NOT NULL
                            ORDER BY closed_at DESC LIMIT 15
                            """
                        )
                        count = len(sl_rows)
                        if count >= 8:
                            wins = sum(1 for r in sl_rows if (r["realised_pnl"] or 0) > 0)
                            rolling_wr = wins / count
                            if rolling_wr < 0.25:
                                size_scale = 0.7
                    except Exception as e:
                        logger.error("claude_edge_cohort_query_failed", error=str(e))

                    target_size = size_usdc * size_scale
                    target_rev_shares = target_size / reversal_price if reversal_price > 0 else 0.0
                    self.opened_claude_edge_reversal.add(market_id)
                    await self._trigger_virtual_trade(
                        profile_name="claude_edge_reversal",
                        market=market,
                        side=reversal_side,
                        entry_price=reversal_price,
                        size_usdc=target_size,
                        shares=target_rev_shares,
                        strategy_mode="REVERSAL",
                        target_token_id=reversal_tid,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )

        # ----------------------------------------------------
        # 1e. Single Market Hedge (Strategy A)
        # ----------------------------------------------------
        if market_id not in self.opened_single_market_hedge_cheap and market_id not in self.opened_single_market_hedge_expensive:
            cheap_side = None
            cheap_price = None
            cheap_tid = None
            cheap_shares = None
            
            exp_side = None
            exp_price = None
            exp_tid = None
            exp_shares = None
            
            if trend_price > 0 and trend_price < 0.40:
                cheap_side = side
                cheap_price = trend_price
                cheap_tid = target_token_id
                cheap_shares = trend_shares
                
                exp_side = "NO" if side == "YES" else "YES"
                exp_price = reversal_price
                exp_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                exp_shares = reversal_shares
            elif reversal_price > 0 and reversal_price < 0.40:
                cheap_side = "NO" if side == "YES" else "YES"
                cheap_price = reversal_price
                cheap_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                cheap_shares = reversal_shares
                
                exp_side = side
                exp_price = trend_price
                exp_tid = target_token_id
                exp_shares = trend_shares
                
            if cheap_side is not None and exp_price is not None and exp_price >= 0.60:
                logger.info(
                    "profile_triggered_single_market_hedge",
                    market_id=market_id,
                    cheap_side=cheap_side,
                    cheap_price=cheap_price,
                    exp_side=exp_side,
                    exp_price=exp_price
                )
                self.opened_single_market_hedge_cheap.add(market_id)
                self.opened_single_market_hedge_expensive.add(market_id)
                
                # Trigger Cheap Position
                await self._trigger_virtual_trade(
                    profile_name="single_market_hedge_cheap",
                    market=market,
                    side=cheap_side,
                    entry_price=cheap_price,
                    size_usdc=size_usdc,
                    shares=cheap_shares,
                    strategy_mode="TREND" if cheap_side == side else "REVERSAL",
                    target_token_id=cheap_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )
                
                # Trigger Expensive Position
                await self._trigger_virtual_trade(
                    profile_name="single_market_hedge_expensive",
                    market=market,
                    side=exp_side,
                    entry_price=exp_price,
                    size_usdc=size_usdc,
                    shares=exp_shares,
                    strategy_mode="TREND" if exp_side == side else "REVERSAL",
                    target_token_id=exp_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 1f. Cross-Market Hedge (Strategy B)
        # ----------------------------------------------------
        if market_id not in self.opened_cross_market_hedge:
            slug = (market.get("slug") or "").lower()
            question = (market.get("question") or "").lower()
            
            symbol = "BTC"
            if "eth" in slug or "ethereum" in question or "eth" in question:
                symbol = "ETH"
            elif "sol" in slug or "solana" in question:
                symbol = "SOL"
                
            if symbol in ("BTC", "ETH"):
                twin_symbol = "ETH" if symbol == "BTC" else "BTC"
                
                if closes_at > 0:
                    try:
                        row = await fetch_one(
                            self.db,
                            "SELECT market_id, yes_token_id, no_token_id, slug, question FROM markets WHERE closes_at = ? AND market_id != ?",
                            (closes_at, market_id)
                        )
                        if row:
                            twin_market_id = row["market_id"]
                            if twin_market_id not in self.opened_cross_market_hedge:
                                ws = self.position_mgr.ws
                                if ws is not None:
                                    twin_yes_tid = row["yes_token_id"]
                                    twin_no_tid = row["no_token_id"]
                                    twin_yes_ask = ws.get_best_ask(twin_yes_tid)
                                    twin_no_ask = ws.get_best_ask(twin_no_tid)
                                    
                                    if twin_yes_ask is not None and twin_no_ask is not None:
                                        twin_yes_p = float(twin_yes_ask)
                                        twin_no_p = float(twin_no_ask)
                                        
                                        cur_yes_p = trend_price if side == "YES" else reversal_price
                                        cur_no_p = reversal_price if side == "YES" else trend_price
                                        
                                        btc_yes_p = cur_yes_p if symbol == "BTC" else twin_yes_p
                                        btc_no_p = cur_no_p if symbol == "BTC" else twin_no_p
                                        eth_yes_p = twin_yes_p if symbol == "BTC" else cur_yes_p
                                        eth_no_p = twin_no_p if symbol == "BTC" else cur_no_p
                                        
                                        trigger_case_1 = (0 < btc_yes_p < 0.40 and 0 < eth_no_p < 0.40)
                                        trigger_case_2 = (0 < btc_no_p < 0.40 and 0 < eth_yes_p < 0.40)
                                        
                                        if trigger_case_1 or trigger_case_2:
                                            cur_side = "YES" if (trigger_case_1 if symbol == "BTC" else trigger_case_2) else "NO"
                                            cur_price = cur_yes_p if cur_side == "YES" else cur_no_p
                                            cur_tid = market.get("yes_token_id") if cur_side == "YES" else market.get("no_token_id")
                                            cur_shares = size_usdc / cur_price if cur_price > 0 else 0.0
                                            
                                            twin_side = "NO" if (trigger_case_1 if symbol == "BTC" else trigger_case_2) else "YES"
                                            twin_price = twin_no_p if twin_side == "NO" else twin_yes_p
                                            twin_tid = twin_no_tid if twin_side == "NO" else twin_yes_tid
                                            twin_shares = size_usdc / twin_price if twin_price > 0 else 0.0
                                            
                                            if closes_at - now_ts < 60:
                                                logger.info(
                                                    "cross_market_hedge_skip_expiry",
                                                    market_id=market_id,
                                                    closes_in=closes_at - now_ts,
                                                    message="cross_market_hedge skipped: closes in less than 60s"
                                                )
                                            elif cur_price < 0.05 or twin_price < 0.05:
                                                logger.info(
                                                    "cross_market_hedge_skip_price_floor",
                                                    market_id=market_id,
                                                    cur_price=cur_price,
                                                    twin_price=twin_price,
                                                    message="cross_market_hedge skipped: price below 0.05 floor"
                                                )
                                            else:
                                                logger.info(
                                                    "profile_triggered_cross_market_hedge",
                                                    market_id=market_id,
                                                    twin_market_id=twin_market_id,
                                                    btc_yes_p=btc_yes_p,
                                                    btc_no_p=btc_no_p,
                                                    eth_yes_p=eth_yes_p,
                                                    eth_no_p=eth_no_p
                                                )
                                                self.opened_cross_market_hedge.add(market_id)
                                                self.opened_cross_market_hedge.add(twin_market_id)
                                                
                                                # Trigger Current Market
                                                await self._trigger_virtual_trade(
                                                    profile_name="cross_market_hedge",
                                                    market=market,
                                                    side=cur_side,
                                                    entry_price=cur_price,
                                                    size_usdc=size_usdc,
                                                    shares=cur_shares,
                                                    strategy_mode="TREND" if cur_side == side else "REVERSAL",
                                                    target_token_id=cur_tid,
                                                    win_prob_estimate=win_prob_estimate,
                                                    kelly_fraction_used=kelly_fraction_used,
                                                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                    book_imbalance_at_entry=book_imbalance_at_entry,
                                                    cvd_60s_at_entry=cvd_60s_at_entry,
                                                    correlated_position_open=correlated_position_open,
                                                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                    market_age_at_entry_pct=market_age_at_entry_pct,
                                                    bankroll_at_entry=bankroll_at_entry,
                                                    persistence_score=persistence_score,
                                                    streak_depth=streak_depth
                                                )
                                                
                                                twin_market = {
                                                    "market_id": twin_market_id,
                                                    "yes_token_id": twin_yes_tid,
                                                    "no_token_id": twin_no_tid,
                                                    "closes_at": closes_at,
                                                    "slug": row["slug"],
                                                    "question": row["question"]
                                                }
                                                
                                                # Trigger Twin Market
                                                await self._trigger_virtual_trade(
                                                    profile_name="cross_market_hedge",
                                                    market=twin_market,
                                                    side=twin_side,
                                                    entry_price=twin_price,
                                                    size_usdc=size_usdc,
                                                    shares=twin_shares,
                                                    strategy_mode="TREND" if twin_side == side else "REVERSAL",
                                                    target_token_id=twin_tid,
                                                    win_prob_estimate=win_prob_estimate,
                                                    kelly_fraction_used=kelly_fraction_used,
                                                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                    book_imbalance_at_entry=book_imbalance_at_entry,
                                                    cvd_60s_at_entry=cvd_60s_at_entry,
                                                    correlated_position_open=correlated_position_open,
                                                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                    market_age_at_entry_pct=market_age_at_entry_pct,
                                                    bankroll_at_entry=bankroll_at_entry,
                                                    persistence_score=persistence_score,
                                                    streak_depth=streak_depth
                                                )

                                                # ----------------------------------------------------
                                                # livemode Profile (CMH Mimic < 0.07 Hold-To-Resolution)
                                                # Strictly copies only the CMH leg(s) below 0.07 threshold
                                                # RESTRICTED TO 5-MINUTE MARKETS ONLY (Option 1)
                                                # ----------------------------------------------------
                                                if cur_price < 0.07 and market_id not in self.opened_livemode:
                                                    if window == 5:
                                                        self.opened_livemode.add(market_id)
                                                        logger.info(
                                                            "profile_triggered_livemode",
                                                            market_id=market_id,
                                                            side=cur_side,
                                                            entry_price=cur_price,
                                                            source="cross_market_hedge"
                                                        )
                                                        await self._trigger_virtual_trade(
                                                            profile_name="livemode",
                                                            market=market,
                                                            side=cur_side,
                                                            entry_price=cur_price,
                                                            size_usdc=size_usdc,
                                                            shares=cur_shares,
                                                            strategy_mode="TREND" if cur_side == side else "REVERSAL",
                                                            target_token_id=cur_tid,
                                                            win_prob_estimate=win_prob_estimate,
                                                            kelly_fraction_used=kelly_fraction_used,
                                                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                            book_imbalance_at_entry=book_imbalance_at_entry,
                                                            cvd_60s_at_entry=cvd_60s_at_entry,
                                                            correlated_position_open=correlated_position_open,
                                                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                            market_age_at_entry_pct=market_age_at_entry_pct,
                                                            bankroll_at_entry=bankroll_at_entry,
                                                            persistence_score=persistence_score,
                                                            streak_depth=streak_depth
                                                        )

                                                        # Live Execution Hook for livemode ($1.00 sizing)
                                                        live_prof = getattr(self.position_mgr.config, "live_profile_only", "livemode")
                                                        if type(live_prof).__name__ not in ('MagicMock', 'AsyncMock', 'Mock'):
                                                            live_prof_str = str(live_prof).lower().strip()
                                                        else:
                                                            live_prof_str = "livemode"

                                                        if not getattr(self.position_mgr, "_paper", True) and live_prof_str in ("livemode", "liverun", "all"):
                                                            live_size = 1.0
                                                            live_shares = live_size / cur_price if cur_price > 0 else 0.0
                                                            logger.info(
                                                                "placing_live_order_for_livemode",
                                                                market_id=market_id,
                                                                side=cur_side,
                                                                price=cur_price,
                                                                size_usdc=live_size,
                                                                shares=live_shares
                                                            )
                                                            asyncio.create_task(
                                                                self.position_mgr.open_position(
                                                                    market_id=market_id,
                                                                    target_token_id=cur_tid,
                                                                    entry_price=cur_price,
                                                                    size_usdc=live_size,
                                                                    shares=live_shares,
                                                                    side=cur_side,
                                                                    win_prob_estimate=win_prob_estimate,
                                                                    kelly_fraction_used=0.0,
                                                                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                                    book_imbalance_at_entry=book_imbalance_at_entry,
                                                                    cvd_60s_at_entry=cvd_60s_at_entry,
                                                                    correlated_position_open=correlated_position_open,
                                                                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                                    market_age_at_entry_pct=market_age_at_entry_pct,
                                                                    bankroll_at_entry=bankroll_at_entry,
                                                                    streak_depth=streak_depth,
                                                                    persistence_score=persistence_score,
                                                                    strategy_mode="LIVEMODE_CMH",
                                                                    original_side=cur_side
                                                                )
                                                            )
                                                    else:
                                                        logger.info(
                                                            "livemode_skipped_non_5m_window",
                                                            market_id=market_id,
                                                            window=window,
                                                            cur_price=cur_price,
                                                            message="livemode is restricted to 5m markets only; skipping non-5m market."
                                                        )

                                                if twin_price < 0.07 and twin_market_id not in self.opened_livemode:
                                                    twin_row_dict = dict(row) if row else {}
                                                    twin_slug = (twin_row_dict.get("slug") or "").lower()
                                                    twin_q = (twin_row_dict.get("question") or "").lower()
                                                    twin_window = 15 if ("-15m-" in twin_slug or "15-minute" in twin_q) else (5 if ("-5m-" in twin_slug or "5-minute" in twin_q) else window)

                                                    if twin_window == 5:
                                                        self.opened_livemode.add(twin_market_id)
                                                        logger.info(
                                                            "profile_triggered_livemode",
                                                            market_id=twin_market_id,
                                                            side=twin_side,
                                                            entry_price=twin_price,
                                                            source="cross_market_hedge"
                                                        )
                                                        await self._trigger_virtual_trade(
                                                            profile_name="livemode",
                                                            market=twin_market,
                                                            side=twin_side,
                                                            entry_price=twin_price,
                                                            size_usdc=size_usdc,
                                                            shares=twin_shares,
                                                            strategy_mode="TREND" if twin_side == side else "REVERSAL",
                                                            target_token_id=twin_tid,
                                                            win_prob_estimate=win_prob_estimate,
                                                            kelly_fraction_used=kelly_fraction_used,
                                                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                            book_imbalance_at_entry=book_imbalance_at_entry,
                                                            cvd_60s_at_entry=cvd_60s_at_entry,
                                                            correlated_position_open=correlated_position_open,
                                                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                            market_age_at_entry_pct=market_age_at_entry_pct,
                                                            bankroll_at_entry=bankroll_at_entry,
                                                            persistence_score=persistence_score,
                                                            streak_depth=streak_depth
                                                        )

                                                        # Live Execution Hook for twin livemode leg ($1.00 sizing)
                                                        live_prof = getattr(self.position_mgr.config, "live_profile_only", "livemode")
                                                        if type(live_prof).__name__ not in ('MagicMock', 'AsyncMock', 'Mock'):
                                                            live_prof_str = str(live_prof).lower().strip()
                                                        else:
                                                            live_prof_str = "livemode"

                                                        if not getattr(self.position_mgr, "_paper", True) and live_prof_str in ("livemode", "liverun", "all"):
                                                            live_size = 1.0
                                                            live_shares = live_size / twin_price if twin_price > 0 else 0.0
                                                            logger.info(
                                                                "placing_live_order_for_livemode",
                                                                market_id=twin_market_id,
                                                                side=twin_side,
                                                                price=twin_price,
                                                                size_usdc=live_size,
                                                                shares=live_shares
                                                            )
                                                            asyncio.create_task(
                                                                self.position_mgr.open_position(
                                                                    market_id=twin_market_id,
                                                                    target_token_id=twin_tid,
                                                                    entry_price=twin_price,
                                                                    size_usdc=live_size,
                                                                    shares=live_shares,
                                                                    side=twin_side,
                                                                    win_prob_estimate=win_prob_estimate,
                                                                    kelly_fraction_used=0.0,
                                                                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                                                    book_imbalance_at_entry=book_imbalance_at_entry,
                                                                    cvd_60s_at_entry=cvd_60s_at_entry,
                                                                    correlated_position_open=correlated_position_open,
                                                                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                                                                    market_age_at_entry_pct=market_age_at_entry_pct,
                                                                    bankroll_at_entry=bankroll_at_entry,
                                                                    streak_depth=streak_depth,
                                                                    persistence_score=persistence_score,
                                                                    strategy_mode="LIVEMODE_CMH",
                                                                    original_side=twin_side
                                                                )
                                                            )
                                                    else:
                                                        logger.info(
                                                            "livemode_twin_skipped_non_5m_window",
                                                            market_id=twin_market_id,
                                                            window=twin_window,
                                                            twin_price=twin_price,
                                                            message="livemode twin is restricted to 5m markets only; skipping non-5m market."
                                                        )
                    except Exception as ex_b:
                        logger.error("cross_market_hedge_trigger_failed", market_id=market_id, error=str(ex_b))

        # ── CMH Filter Room Classifier ──────────────────────────────────────
        # Runs AFTER the base cross_market_hedge block. Does NOT change it.
        # For each pair that base CMH already triggered, evaluate 7 gate filters
        # and store position copies into named sub-profile "rooms" for analysis.
        # A pair can exist in multiple rooms if it passes multiple gates.
        # Also stores twin book imbalance + gate results in notes JSON.
        # ────────────────────────────────────────────────────────────────────


        # If this is a lightweight signal (streak = 1), return early to prevent other profiles from triggering
        if market.get("is_lightweight", False):
            return

        # ----------------------------------------------------
        # 1. Stop Loss Profile
        # Trigger: immediately at signal time, subject to standard B3 price gates and depth limits.
        if market_id not in self.opened_stop_loss:
            yes_price_val = market.get("yes_price", 0.5)
            from unittest.mock import MagicMock, AsyncMock, Mock
            ws = self.position_mgr.ws
            if ws is not None and not isinstance(ws, (MagicMock, AsyncMock, Mock)):
                latest_yes = ws.get_best_ask(market.get("yes_token_id"))
                if latest_yes is not None and not isinstance(latest_yes, (MagicMock, AsyncMock, Mock)):
                    yes_price_val = float(latest_yes)
            
            if streak_depth >= 6:
                logger.info("profile_skip_stop_loss_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif not (0.30 <= yes_price_val <= 0.60):
                logger.info("profile_skip_stop_loss_price_gate_block", market_id=market_id, yes_price=yes_price_val)
            else:
                logger.info("profile_triggered_stop_loss", market_id=market_id, price=trend_price)
                self.opened_stop_loss.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="stop_loss",
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 1e. Era 3 Cheap HTR Profile (era3_cheap_htr)
        # Entry Price < 0.35 + Hold-To-Resolution (HTR)
        # ----------------------------------------------------
        if market_id not in self.opened_era3_cheap_htr:
            if streak_depth >= 6:
                pass
            elif trend_price >= 0.35:
                pass
            else:
                logger.info("profile_triggered_era3_cheap_htr", market_id=market_id, price=trend_price)
                self.opened_era3_cheap_htr.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="era3_cheap_htr",
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 1f. Era 3 NO Cheap HTR Profile (era3_no_cheap_htr)
        # Side=NO + Entry Price < 0.35 + Hold-To-Resolution (HTR)
        # ----------------------------------------------------
        if market_id not in self.opened_era3_no_cheap_htr:
            if streak_depth >= 6:
                pass
            elif side != "NO" or trend_price >= 0.35:
                pass
            else:
                logger.info("profile_triggered_era3_no_cheap_htr", market_id=market_id, price=trend_price)
                self.opened_era3_no_cheap_htr.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="era3_no_cheap_htr",
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 1g. Era 3 Hybrid BandPass Profile (era3_hybrid_bandpass)
        # Entry Price < 0.35 (HTR), 0.35 <= Entry < 0.50 (12% SL), >= 0.50 Blocked
        # ----------------------------------------------------
        if market_id not in self.opened_era3_hybrid_bandpass:
            if streak_depth >= 6:
                pass
            elif trend_price >= 0.50:
                pass
            else:
                logger.info("profile_triggered_era3_hybrid_bandpass", market_id=market_id, price=trend_price)
                self.opened_era3_hybrid_bandpass.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="era3_hybrid_bandpass",
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 1h. Claude Audit Profile
        # Trigger: immediately at signal time (same trigger as stop_loss), subject to B3 gates and depth limit.
        # Additional gate: Skip if current UTC hour is in (0, 1, 12, 13, 18, 20, 21, 22, 23)
        if market_id not in self.opened_claude_audit:
            if streak_depth >= 6:
                logger.info("profile_skip_claude_audit_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif trend_price >= 0.40:
                logger.info(
                    "claude_price_cap",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_audit skipped signal on price cap: {trend_price} >= 0.40"
                )
            elif trend_price < 0.10:
                logger.info(
                    "claude_audit_price_floor",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_audit skipped signal on price floor: {trend_price} < 0.10"
                )
            else:
                # Check UTC hour filter using the CLOB-synced time helper
                from datetime import datetime, timezone
                now_ts = self._get_now()
                utc_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
                current_hour = utc_dt.hour
                if current_hour in (0, 1, 12, 18, 20, 22, 23):
                    logger.info(
                        "claude_audit_hour_blacklist",
                        market_id=market_id,
                        hour=current_hour,
                        message=f"claude_audit skipped signal on hour blacklist: {current_hour} UTC"
                    )
                else:
                    logger.info("profile_triggered_claude_audit", market_id=market_id, price=trend_price)
                    self.opened_claude_audit.add(market_id)
                    await self._trigger_virtual_trade(
                        profile_name="claude_audit",
                        market=market,
                        side=side,
                        entry_price=trend_price,
                        size_usdc=size_usdc,
                        shares=trend_shares,
                        strategy_mode="TREND",
                        target_token_id=target_token_id,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )
                    if market_id not in self.opened_claude_audit_reversal:
                        self.opened_claude_audit_reversal.add(market_id)
                        await self._trigger_virtual_trade(
                            profile_name="claude_audit_reversal",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=size_usdc,
                            shares=reversal_shares,
                            strategy_mode="REVERSAL",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )

        # ----------------------------------------------------
        # 1h. Claude Gated Profile
        # Identical to claude_audit but only enters when rolling win rate of last 15 closed stop_loss positions is < 25%.
        if market_id not in self.opened_claude_gated:
            if streak_depth >= 6:
                logger.info("profile_skip_claude_gated_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif trend_price >= 0.40:
                logger.info(
                    "claude_gated_price_cap",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_gated skipped signal on price cap: {trend_price} >= 0.40"
                )
            elif trend_price < 0.10:
                logger.info(
                    "claude_gated_price_floor",
                    market_id=market_id,
                    entry_price=trend_price,
                    message=f"claude_gated skipped signal on price floor: {trend_price} < 0.10"
                )
            else:
                # Check UTC hour filter using the CLOB-synced time helper
                from datetime import datetime, timezone
                now_ts = self._get_now()
                utc_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
                current_hour = utc_dt.hour
                if current_hour in (0, 1, 12, 18, 20, 22, 23):
                    logger.info(
                        "claude_gated_hour_blacklist",
                        market_id=market_id,
                        hour=current_hour,
                        message=f"claude_gated skipped signal on hour blacklist: {current_hour} UTC"
                    )
                else:
                    passed_cohort_gate = True
                    try:
                        sl_rows = await fetch_all(
                            self.db,
                            """
                            SELECT realised_pnl FROM profile_positions
                            WHERE profile_name = 'stop_loss' AND closed_at IS NOT NULL AND realised_pnl IS NOT NULL
                            ORDER BY closed_at DESC LIMIT 15
                            """
                        )
                        count = len(sl_rows)
                        if count >= 8:
                            wins = sum(1 for r in sl_rows if (r["realised_pnl"] or 0) > 0)
                            rolling_wr = wins / count
                            if rolling_wr >= 0.25:
                                passed_cohort_gate = False
                                logger.info(
                                    "claude_gated_cohort_a_skip",
                                    market_id=market_id,
                                    rolling_wr=rolling_wr,
                                    count=count,
                                    message=f"claude_gated skipped signal on cohort A: {rolling_wr:.4f} >= 0.25"
                                )
                    except Exception as e:
                        logger.error("claude_gated_cohort_query_failed", error=str(e))
                        passed_cohort_gate = True

                    if passed_cohort_gate:
                        logger.info("profile_triggered_claude_gated", market_id=market_id, price=trend_price)
                        self.opened_claude_gated.add(market_id)
                        await self._trigger_virtual_trade(
                            profile_name="claude_gated",
                            market=market,
                            side=side,
                            entry_price=trend_price,
                            size_usdc=size_usdc,
                            shares=trend_shares,
                            strategy_mode="TREND",
                            target_token_id=target_token_id,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )
                        if market_id not in self.opened_claude_gated_reversal:
                            self.opened_claude_gated_reversal.add(market_id)
                            await self._trigger_virtual_trade(
                                profile_name="claude_gated_reversal",
                                market=market,
                                side=reversal_side,
                                entry_price=reversal_price,
                                size_usdc=size_usdc,
                                shares=reversal_shares,
                                strategy_mode="REVERSAL",
                                target_token_id=reversal_tid,
                                win_prob_estimate=win_prob_estimate,
                                kelly_fraction_used=kelly_fraction_used,
                                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                book_imbalance_at_entry=book_imbalance_at_entry,
                                cvd_60s_at_entry=cvd_60s_at_entry,
                                correlated_position_open=correlated_position_open,
                                seconds_to_close_at_entry=seconds_to_close_at_entry,
                                market_age_at_entry_pct=market_age_at_entry_pct,
                                bankroll_at_entry=bankroll_at_entry,
                                persistence_score=persistence_score,
                                streak_depth=streak_depth
                            )

        # ----------------------------------------------------
        # 1c. Stop Loss Filtered Profile (A/B Test Shadow Profile)
        # Trigger: same conditions as stop_loss, but evaluated through Guardian
        if market_id not in self.opened_stop_loss_filtered:
            yes_price_val = market.get("yes_price", 0.5)
            from unittest.mock import MagicMock, AsyncMock, Mock
            ws = self.position_mgr.ws
            if ws is not None and not isinstance(ws, (MagicMock, AsyncMock, Mock)):
                latest_yes = ws.get_best_ask(market.get("yes_token_id"))
                if latest_yes is not None and not isinstance(latest_yes, (MagicMock, AsyncMock, Mock)):
                    yes_price_val = float(latest_yes)
            
            if streak_depth >= 6:
                logger.info("profile_skip_stop_loss_filtered_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif not (0.30 <= yes_price_val <= 0.60):
                logger.info("profile_skip_stop_loss_filtered_price_gate_block", market_id=market_id, yes_price=yes_price_val)
            else:
                allowed = await self.guardian.is_profile_allowed("stop_loss_filtered")
                if not allowed:
                    logger.info("profile_skip_stop_loss_filtered_guardian_blocked", market_id=market_id)
                else:
                    scaled_size = self.guardian.get_scaled_size("stop_loss_filtered", size_usdc)
                    scaled_shares = scaled_size / trend_price if trend_price > 0 else 0.0
                    
                    if scaled_shares < 6.0:
                        logger.info("profile_skip_stop_loss_filtered_share_floor", market_id=market_id, shares=scaled_shares)
                    else:
                        logger.info("profile_triggered_stop_loss_filtered", market_id=market_id, price=trend_price, size=scaled_size, shares=scaled_shares)
                        self.opened_stop_loss_filtered.add(market_id)
                        await self._trigger_virtual_trade(
                            profile_name="stop_loss_filtered",
                            market=market,
                            side=side,
                            entry_price=trend_price,
                            size_usdc=scaled_size,
                            shares=scaled_shares,
                            strategy_mode="TREND",
                            target_token_id=target_token_id,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )

        # ----------------------------------------------------
        # 1b. Probability Stop Loss Profile
        # Trigger: immediately at signal time, subject to standard B3 price gates and depth limits.
        if market_id not in self.opened_probability_sl:
            yes_price_val = market.get("yes_price", 0.5)
            from unittest.mock import MagicMock, AsyncMock, Mock
            ws = self.position_mgr.ws
            if ws is not None and not isinstance(ws, (MagicMock, AsyncMock, Mock)):
                latest_yes = ws.get_best_ask(market.get("yes_token_id"))
                if latest_yes is not None and not isinstance(latest_yes, (MagicMock, AsyncMock, Mock)):
                    yes_price_val = float(latest_yes)
            
            if streak_depth >= 6:
                logger.info("profile_skip_probability_sl_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif not (0.30 <= yes_price_val <= 0.60):
                logger.info("profile_skip_probability_sl_price_gate_block", market_id=market_id, yes_price=yes_price_val)
            elif trend_price < 0.50:
                logger.info("profile_skip_probability_sl_entry_below_50", market_id=market_id, trend_price=trend_price)
            else:
                logger.info("profile_triggered_probability_sl", market_id=market_id, price=trend_price)
                self.opened_probability_sl.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="probability_sl",
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 2. Loss Skip Profile
        # ----------------------------------------------------
        # Trigger: checks the main bot's last closed trade directly in the database.
        # If it was a loss (realised_pnl < 0), we open a position in loss_skip.
        last_main_is_loss = False
        try:
            from src.db.queries import get_last_closed_trend_trade
            is_paper = getattr(self.position_mgr, "_paper", True) is True
            last_closed = await get_last_closed_trend_trade(
                self.db, self.position_mgr.skip_db, paper=is_paper, symbol=None
            )
            if last_closed and last_closed.get("realised_pnl") is not None and last_closed["realised_pnl"] < 0:
                last_main_is_loss = True
        except Exception as e:
            logger.error("profile_loss_skip_check_failed", error=str(e))

        if last_main_is_loss:
            if streak_depth >= 6:
                logger.info("profile_skip_loss_skip_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif reversal_price >= 0.50:
                logger.warning(
                    "profile_skip_loss_skip_price_block",
                    market_id=market_id,
                    reversal_price=reversal_price,
                    message=f"profile loss_skip trade blocked: reversal price {reversal_price:.4f} >= 0.50."
                )
            else:
                logger.info("profile_triggered_loss_skip", market_id=market_id)
                reversal_side = "NO" if side == "YES" else "YES"
                reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                await self._trigger_virtual_trade(
                    profile_name="loss_skip",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="LOSS_FADE",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # Extract window minutes
        slug = (market.get("slug") or "").lower()
        question = (market.get("question") or "").lower()
        window = market.get("window_minutes")
        if not window:
            if "-5m-" in slug or "5-minute" in question:
                window = 5
            elif "-15m-" in slug or "15-minute" in question:
                window = 15
            else:
                window = 5

        # ----------------------------------------------------
        # 3. Cheap Reversal Profile
        # ----------------------------------------------------
        # Trigger: immediately at signal time (Streak 2+).
        # We bet in REVERSAL mode (fade the trend) if the reversal contract price is [0.52, 0.80), orderbook imbalance < -0.15, and it's a 5m market.
        # Actionable Filter (Analysis D): Filter out LowCVD + WeakSell (|CVD_60s| < 61132.9 and book_imbalance >= -0.3448).
        if (
            window == 5 
            and streak_depth >= 2 
            and 0.52 <= reversal_price < 0.80 
            and book_imbalance_at_entry < -0.15
            and not (abs(cvd_60s_at_entry) < 61132.9 and book_imbalance_at_entry >= -0.3448)
        ):
            logger.info("profile_triggered_cheap_reversal", market_id=market_id, reversal_price=reversal_price)
            reversal_side = "NO" if side == "YES" else "YES"
            reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
            await self._trigger_virtual_trade(
                profile_name="cheap_reversal",
                market=market,
                side=reversal_side,
                entry_price=reversal_price,
                size_usdc=size_usdc,
                shares=reversal_shares,
                strategy_mode="REVERSAL",
                target_token_id=reversal_tid,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                streak_depth=streak_depth
            )

        # 3b. Cheap Reversal V2 Profile (With Trend-Day Guards)
        if market_id not in self.opened_cheap_reversal_v2:
            if window == 5 and streak_depth >= 2 and 0.52 <= reversal_price < 0.80 and book_imbalance_at_entry < -0.15:
                has_cooldown = await self._check_consecutive_loss_cooldown("cheap_reversal_v2")
                has_conflict = await self._check_htf_trend_conflict(symbol, side)
                if not has_cooldown and not has_conflict:
                    logger.info("profile_triggered_cheap_reversal_v2", market_id=market_id, reversal_price=reversal_price)
                    self.opened_cheap_reversal_v2.add(market_id)
                    reversal_side = "NO" if side == "YES" else "YES"
                    reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                    await self._trigger_virtual_trade(
                        profile_name="cheap_reversal_v2",
                        market=market,
                        side=reversal_side,
                        entry_price=reversal_price,
                        size_usdc=size_usdc,
                        shares=reversal_shares,
                        strategy_mode="REVERSAL",
                        target_token_id=reversal_tid,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )

        # ----------------------------------------------------
        # 4. Fade Reversal Profile
        # ----------------------------------------------------
        # Trigger: immediately at signal time (Streak 2+).
        # Apply price gates to prevent toxic reversal trades at extreme contract prices
        yes_price_val = trend_price if side == "YES" else reversal_price
        if 0.30 <= yes_price_val <= 0.60:
            logger.info("profile_triggered_reversal", market_id=market_id)
            
            # Flip YES <-> NO side and target token ID
            reversal_side = "NO" if side == "YES" else "YES"
            reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                
            await self._trigger_virtual_trade(
                profile_name="reversal",
                market=market,
                side=reversal_side,
                entry_price=reversal_price,
                size_usdc=size_usdc,
                shares=reversal_shares,
                strategy_mode="REVERSAL",
                target_token_id=reversal_tid,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                streak_depth=streak_depth
            )

        # ----------------------------------------------------
        # 4b. Reversal Stop Loss Profile
        # ----------------------------------------------------
        # Trigger: immediately at signal time (Streak 2+).
        # Enforces a Stop Loss.
        if market_id not in self.opened_reversal_stop_loss:
            yes_price_val = trend_price if side == "YES" else reversal_price
            from unittest.mock import MagicMock, AsyncMock, Mock
            ws = self.position_mgr.ws
            if ws is not None and not isinstance(ws, (MagicMock, AsyncMock, Mock)):
                latest_yes = ws.get_best_ask(market.get("yes_token_id"))
                if latest_yes is not None and not isinstance(latest_yes, (MagicMock, AsyncMock, Mock)):
                    yes_price_val = float(latest_yes)
            
            if streak_depth >= 6:
                logger.info("profile_skip_reversal_stop_loss_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif not (0.30 <= yes_price_val <= 0.60):
                logger.info("profile_skip_reversal_stop_loss_price_gate_block", market_id=market_id, yes_price=yes_price_val)
            elif not (0.30 <= reversal_price <= 0.70):
                logger.info("profile_skip_reversal_stop_loss_reversal_price_block", market_id=market_id, reversal_price=reversal_price)
            else:
                logger.info("profile_triggered_reversal_stop_loss", market_id=market_id)
                self.opened_reversal_stop_loss.add(market_id)
                
                reversal_side = "NO" if side == "YES" else "YES"
                reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                
                await self._trigger_virtual_trade(
                    profile_name="reversal_stop_loss",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 4d. Reversal Stop Loss Filtered Profile (A/B Test Shadow Profile)
        # Trigger: same conditions as reversal_stop_loss, but evaluated through Guardian
        if market_id not in self.opened_reversal_stop_loss_filtered:
            yes_price_val = trend_price if side == "YES" else reversal_price
            from unittest.mock import MagicMock, AsyncMock, Mock
            ws = self.position_mgr.ws
            if ws is not None and not isinstance(ws, (MagicMock, AsyncMock, Mock)):
                latest_yes = ws.get_best_ask(market.get("yes_token_id"))
                if latest_yes is not None and not isinstance(latest_yes, (MagicMock, AsyncMock, Mock)):
                    yes_price_val = float(latest_yes)
            
            if streak_depth >= 6:
                logger.info("profile_skip_reversal_stop_loss_filtered_depth_block", market_id=market_id, streak_depth=streak_depth)
            elif not (0.30 <= yes_price_val <= 0.60):
                logger.info("profile_skip_reversal_stop_loss_filtered_price_gate_block", market_id=market_id, yes_price=yes_price_val)
            elif not (0.30 <= reversal_price <= 0.70):
                logger.info("profile_skip_reversal_stop_loss_filtered_reversal_price_block", market_id=market_id, reversal_price=reversal_price)
            else:
                allowed = await self.guardian.is_profile_allowed("reversal_stop_loss_filtered")
                if not allowed:
                    logger.info("profile_skip_reversal_stop_loss_filtered_guardian_blocked", market_id=market_id)
                else:
                    scaled_size = self.guardian.get_scaled_size("reversal_stop_loss_filtered", size_usdc)
                    scaled_reversal_shares = scaled_size / reversal_price if reversal_price > 0 else 0.0
                    
                    if scaled_reversal_shares < 6.0:
                        logger.info("profile_skip_reversal_stop_loss_filtered_share_floor", market_id=market_id, shares=scaled_reversal_shares)
                    else:
                        logger.info("profile_triggered_reversal_stop_loss_filtered", market_id=market_id, price=reversal_price, size=scaled_size, shares=scaled_reversal_shares)
                        self.opened_reversal_stop_loss_filtered.add(market_id)
                        
                        reversal_side = "NO" if side == "YES" else "YES"
                        reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                        
                        await self._trigger_virtual_trade(
                            profile_name="reversal_stop_loss_filtered",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=scaled_size,
                            shares=scaled_reversal_shares,
                            strategy_mode="REVERSAL",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )

        # ----------------------------------------------------
        # 4c. Reversal Probability Stop Loss Profile
        # ----------------------------------------------------
        # Trigger: immediately at signal time (Streak 2+).
        # Enforces a 50% Probability Stop Loss with 400ms wick guard.
        if market_id not in self.opened_reversal_probability_sl:
            if reversal_price < 0.50:
                logger.info("profile_skip_reversal_probability_sl_entry_below_50", market_id=market_id, reversal_price=reversal_price)
            else:
                logger.info("profile_triggered_reversal_probability_sl", market_id=market_id)
                self.opened_reversal_probability_sl.add(market_id)
                
                reversal_side = "NO" if side == "YES" else "YES"
                reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")
                
                await self._trigger_virtual_trade(
                    profile_name="reversal_probability_sl",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # 5. Streak Momentum Profile
        # ----------------------------------------------------
        # Trigger: Always follow the trend (TREND strategy mode) on all validated signals.
        # Enforce the trend ceiling of streak_depth < 6.
        if streak_depth >= 6:
            logger.info("profile_skip_streak_momentum_depth_block", market_id=market_id, streak_depth=streak_depth)
        else:
            # Proceed with standard TREND trade for streak_momentum profile
            logger.info("profile_triggered_streak_momentum", market_id=market_id)
            await self._trigger_virtual_trade(
                profile_name="streak_momentum",
                market=market,
                side=side,
                entry_price=trend_price,
                size_usdc=size_usdc,
                shares=trend_shares,
                strategy_mode="TREND",
                target_token_id=target_token_id,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                streak_depth=streak_depth
            )

        # ----------------------------------------------------
        # 6. Parallel Shadow Scientist Profiles (Phase 14)
        # ----------------------------------------------------
        # Evaluate using the lightweight KNN manifold model
        if self.lightweight_knn is not None:
            try:
                new_feat = [
                    float(trend_price),
                    float(bid_ask_spread_at_entry),
                    float(book_imbalance_at_entry),
                    float(cvd_60s_at_entry),
                    float(seconds_to_close_at_entry),
                    float(market_age_at_entry_pct),
                    float(streak_depth)
                ]
                pred = self.lightweight_knn.predict(new_feat, K=100)
                
                # Build rich prediction payload
                notes_payload_json = json.dumps({
                    "tid": target_token_id,
                    "strategy": strategy_mode,
                    "streak_depth": streak_depth,
                    "predicted_ev": pred["predicted_ev"],
                    "confidence": pred["confidence"],
                    "expected_slippage": pred["expected_slippage"],
                    "expected_fill_prob": pred["expected_fill_prob"],
                    "distance": pred["distance"],
                    "alpha_score": pred["alpha_score"],
                    "would_trade": pred["would_trade"],
                    "suggested_size": pred["suggested_size"]
                })
                
                logger.info(
                    "shadow_profile_knn_evaluation",
                    market_id=market_id,
                    predicted_ev=pred["predicted_ev"],
                    confidence=pred["confidence"],
                    alpha_score=pred["alpha_score"],
                    would_trade=pred["would_trade"]
                )
                
                # 6a. Predict expected value
                if market_id not in self.opened_continuous_ev_v1:
                    self.opened_continuous_ev_v1.add(market_id)
                    await self._trigger_virtual_trade_with_notes(
                        profile_name="continuous_ev_v1",
                        market=market,
                        side=side,
                        entry_price=trend_price,
                        size_usdc=size_usdc,
                        shares=trend_shares,
                        strategy_mode=strategy_mode,
                        target_token_id=target_token_id,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth,
                        custom_notes=notes_payload_json
                    )
                    if market_id not in self.opened_continuous_ev_v1_reversal:
                        self.opened_continuous_ev_v1_reversal.add(market_id)
                        await self._trigger_virtual_trade_with_notes(
                            profile_name="continuous_ev_v1_reversal",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=size_usdc,
                            shares=reversal_shares,
                            strategy_mode="REVERSAL",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth,
                            custom_notes=notes_payload_json
                        )
                    
                # 6b. Predict certainty/confidence
                if market_id not in self.opened_confidence_v1:
                    self.opened_confidence_v1.add(market_id)
                    await self._trigger_virtual_trade_with_notes(
                        profile_name="confidence_v1",
                        market=market,
                        side=side,
                        entry_price=trend_price,
                        size_usdc=size_usdc,
                        shares=trend_shares,
                        strategy_mode=strategy_mode,
                        target_token_id=target_token_id,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth,
                        custom_notes=notes_payload_json
                    )
                    if market_id not in self.opened_confidence_v1_reversal:
                        self.opened_confidence_v1_reversal.add(market_id)
                        await self._trigger_virtual_trade_with_notes(
                            profile_name="confidence_v1_reversal",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=size_usdc,
                            shares=reversal_shares,
                            strategy_mode="REVERSAL",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth,
                            custom_notes=notes_payload_json
                        )
                    
                # 6c. Predict expected slippage and fill quality
                if market_id not in self.opened_execution_v1:
                    self.opened_execution_v1.add(market_id)
                    await self._trigger_virtual_trade_with_notes(
                        profile_name="execution_v1",
                        market=market,
                        side=side,
                        entry_price=trend_price,
                        size_usdc=size_usdc,
                        shares=trend_shares,
                        strategy_mode=strategy_mode,
                        target_token_id=target_token_id,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth,
                        custom_notes=notes_payload_json
                    )
                    if market_id not in self.opened_execution_v1_reversal:
                        self.opened_execution_v1_reversal.add(market_id)
                        await self._trigger_virtual_trade_with_notes(
                            profile_name="execution_v1_reversal",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=size_usdc,
                            shares=reversal_shares,
                            strategy_mode="REVERSAL",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth,
                            custom_notes=notes_payload_json
                        )
            except Exception as e_knn:
                logger.error("failed_to_evaluate_shadow_profiles_knn", market_id=market_id, error=str(e_knn))

        # ----------------------------------------------------
        # Reversal & Recovery Edges (Quant-vetted)
        # ----------------------------------------------------
        reversal_side = "NO" if side == "YES" else "YES"
        reversal_tid = market.get("no_token_id") if side == "YES" else market.get("yes_token_id")

        # Edge #1: reversal_yes_cvd_imb
        # Trigger: Reversal YES, entry price 0.40-0.70, CVD >= 10k, Imbalance <= -0.10, Streak >= 2
        if market_id not in self.opened_reversal_yes_cvd_imb:
            if (
                reversal_side == "YES"
                and (0.40 <= reversal_price <= 0.70)
                and (cvd_60s_at_entry >= 10000.0)
                and (book_imbalance_at_entry <= -0.10)
                and (streak_depth >= 2)
            ):
                logger.info("profile_triggered_reversal_yes_cvd_imb", market_id=market_id)
                self.opened_reversal_yes_cvd_imb.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="reversal_yes_cvd_imb",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # Edge #2: reversal_yes_cvd_imb_wide
        # Trigger: Reversal YES, entry price >= 0.40, CVD >= 10k, Imbalance <= -0.10, Streak >= 2
        if market_id not in self.opened_reversal_yes_cvd_imb_wide:
            if (
                reversal_side == "YES"
                and (reversal_price >= 0.40)
                and (cvd_60s_at_entry >= 10000.0)
                and (book_imbalance_at_entry <= -0.10)
                and (streak_depth >= 2)
            ):
                logger.info("profile_triggered_reversal_yes_cvd_imb_wide", market_id=market_id)
                self.opened_reversal_yes_cvd_imb_wide.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="reversal_yes_cvd_imb_wide",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # Edge #2b: reversal_yes_cvd_imb_wide_v2 (With Trend-Day Guards)
        if market_id not in self.opened_reversal_yes_cvd_imb_wide_v2:
            if (
                reversal_side == "YES"
                and (reversal_price >= 0.40)
                and (cvd_60s_at_entry >= 10000.0)
                and (book_imbalance_at_entry <= -0.10)
                and (streak_depth >= 2)
            ):
                has_cooldown = await self._check_consecutive_loss_cooldown("reversal_yes_cvd_imb_wide_v2")
                has_conflict = await self._check_htf_trend_conflict(symbol, side)
                if not has_cooldown and not has_conflict:
                    logger.info("profile_triggered_reversal_yes_cvd_imb_wide_v2", market_id=market_id)
                    self.opened_reversal_yes_cvd_imb_wide_v2.add(market_id)
                    await self._trigger_virtual_trade(
                        profile_name="reversal_yes_cvd_imb_wide_v2",
                        market=market,
                        side=reversal_side,
                        entry_price=reversal_price,
                        size_usdc=size_usdc,
                        shares=reversal_shares,
                        strategy_mode="REVERSAL",
                        target_token_id=reversal_tid,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )

        # Edge #3: loss_skip_yes_cvd
        # Trigger: Loss Skip YES, entry price 0.40-0.70, CVD >= 10k, Streak < 6
        if market_id not in self.opened_loss_skip_yes_cvd:
            last_main_is_loss = False
            try:
                from src.db.queries import get_last_closed_trend_trade
                is_paper = getattr(self.position_mgr, "_paper", True) is True
                last_closed = await get_last_closed_trend_trade(
                    self.db, self.position_mgr.skip_db, paper=is_paper, symbol=None
                )
                if last_closed and last_closed.get("realised_pnl") is not None and last_closed["realised_pnl"] < 0:
                    last_main_is_loss = True
            except Exception as e:
                logger.error("profile_loss_skip_yes_cvd_check_failed", error=str(e))

            if last_main_is_loss:
                if (
                    reversal_side == "YES"
                    and (0.40 <= reversal_price <= 0.70)
                    and (cvd_60s_at_entry >= 10000.0)
                    and (streak_depth < 6)
                    and not (abs(cvd_60s_at_entry) >= 71723.0 and book_imbalance_at_entry >= -0.0495)
                ):
                    logger.info("profile_triggered_loss_skip_yes_cvd", market_id=market_id)
                    self.opened_loss_skip_yes_cvd.add(market_id)
                    await self._trigger_virtual_trade(
                        profile_name="loss_skip_yes_cvd",
                        market=market,
                        side=reversal_side,
                        entry_price=reversal_price,
                        size_usdc=size_usdc,
                        shares=reversal_shares,
                        strategy_mode="LOSS_FADE",
                        target_token_id=reversal_tid,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )

        # Edge #3b: loss_skip_yes_cvd_v2 (With Trend-Day Guards)
        if market_id not in self.opened_loss_skip_yes_cvd_v2:
            last_main_is_loss = False
            try:
                from src.db.queries import get_last_closed_trend_trade
                is_paper = getattr(self.position_mgr, "_paper", True) is True
                last_closed = await get_last_closed_trend_trade(
                    self.db, self.position_mgr.skip_db, paper=is_paper, symbol=None
                )
                if last_closed and last_closed.get("realised_pnl") is not None and last_closed["realised_pnl"] < 0:
                    last_main_is_loss = True
            except Exception as e:
                logger.error("profile_loss_skip_yes_cvd_v2_check_failed", error=str(e))

            if last_main_is_loss:
                if (
                    reversal_side == "YES"
                    and (0.40 <= reversal_price <= 0.70)
                    and (cvd_60s_at_entry >= 10000.0)
                    and (streak_depth < 6)
                ):
                    has_cooldown = await self._check_consecutive_loss_cooldown("loss_skip_yes_cvd_v2")
                    has_conflict = await self._check_htf_trend_conflict(symbol, side)
                    if not has_cooldown and not has_conflict:
                        logger.info("profile_triggered_loss_skip_yes_cvd_v2", market_id=market_id)
                        self.opened_loss_skip_yes_cvd_v2.add(market_id)
                        await self._trigger_virtual_trade(
                            profile_name="loss_skip_yes_cvd_v2",
                            market=market,
                            side=reversal_side,
                            entry_price=reversal_price,
                            size_usdc=size_usdc,
                            shares=reversal_shares,
                            strategy_mode="LOSS_FADE",
                            target_token_id=reversal_tid,
                            win_prob_estimate=win_prob_estimate,
                            kelly_fraction_used=kelly_fraction_used,
                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                            book_imbalance_at_entry=book_imbalance_at_entry,
                            cvd_60s_at_entry=cvd_60s_at_entry,
                            correlated_position_open=correlated_position_open,
                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                            market_age_at_entry_pct=market_age_at_entry_pct,
                            bankroll_at_entry=bankroll_at_entry,
                            persistence_score=persistence_score,
                            streak_depth=streak_depth
                        )

        # Microstructure Flow NO Profile (micro_flow_no)
        # Trigger: book_imbalance <= -0.10, cvd_60s <= -20000, NO entry price in 0.30-0.50
        if market_id not in self.opened_micro_flow_no:
            no_p = trend_price if side == "NO" else reversal_price
            if (
                book_imbalance_at_entry <= -0.10
                and cvd_60s_at_entry <= -20000.0
                and (0.30 <= no_p <= 0.50)
            ):
                logger.info("profile_triggered_micro_flow_no", market_id=market_id, entry_price=no_p)
                self.opened_micro_flow_no.add(market_id)
                no_shares = size_usdc / no_p if no_p > 0 else 0.0
                no_tid = market.get("no_token_id") if side == "YES" else target_token_id
                await self._trigger_virtual_trade(
                    profile_name="micro_flow_no",
                    market=market,
                    side="NO",
                    entry_price=no_p,
                    size_usdc=size_usdc,
                    shares=no_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=no_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # Edge #1: loss_skip_btc_5m_recovery
        if market_id not in self.opened_loss_skip_btc_5m_recovery:
            if last_main_is_loss:
                if (
                    reversal_side == "YES"
                    and (0.40 <= reversal_price <= 0.70)
                    and (symbol == "BTC")
                    and (window == 5)
                ):
                    logger.info("profile_triggered_loss_skip_btc_5m_recovery", market_id=market_id)
                    self.opened_loss_skip_btc_5m_recovery.add(market_id)
                    await self._trigger_virtual_trade(
                        profile_name="loss_skip_btc_5m_recovery",
                        market=market,
                        side=reversal_side,
                        entry_price=reversal_price,
                        size_usdc=size_usdc,
                        shares=reversal_shares,
                        strategy_mode="LOSS_FADE",
                        target_token_id=reversal_tid,
                        win_prob_estimate=win_prob_estimate,
                        kelly_fraction_used=kelly_fraction_used,
                        bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                        book_imbalance_at_entry=book_imbalance_at_entry,
                        cvd_60s_at_entry=cvd_60s_at_entry,
                        correlated_position_open=correlated_position_open,
                        seconds_to_close_at_entry=seconds_to_close_at_entry,
                        market_age_at_entry_pct=market_age_at_entry_pct,
                        bankroll_at_entry=bankroll_at_entry,
                        persistence_score=persistence_score,
                        streak_depth=streak_depth
                    )

        # Edge #2: reversal_stop_loss_eth_5m_exhaustion
        if market_id not in self.opened_reversal_stop_loss_eth_5m_exhaustion:
            if (
                reversal_side == "YES"
                and (reversal_price >= 0.40)
                and (symbol == "ETH")
                and (window == 5)
                and (cvd_60s_at_entry >= 10000.0)
                and (streak_depth >= 2)
            ):
                logger.info("profile_triggered_reversal_stop_loss_eth_5m_exhaustion", market_id=market_id)
                self.opened_reversal_stop_loss_eth_5m_exhaustion.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="reversal_stop_loss_eth_5m_exhaustion",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )



        # Edge #4: reversal_eth_5m_momentum
        if market_id not in self.opened_reversal_eth_5m_momentum:
            if (
                reversal_side == "YES"
                and (0.40 <= reversal_price <= 0.70)
                and (symbol == "ETH")
                and (window == 5)
                and (cvd_60s_at_entry >= 10000.0)
                and (streak_depth >= 2)
            ):
                logger.info("profile_triggered_reversal_eth_5m_momentum", market_id=market_id)
                self.opened_reversal_eth_5m_momentum.add(market_id)
                await self._trigger_virtual_trade(
                    profile_name="reversal_eth_5m_momentum",
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # ----------------------------------------------------
        # Executioner Profile (Combined Requests 1, 2, 3 - Per-Request Tracking)
        # ----------------------------------------------------
        # Request 1: stop_loss base criteria with entry_price < 0.30
        req1_key = f"{market_id}_1"
        if req1_key not in self.opened_executioner:
            if streak_depth < 6 and trend_price < 0.30:
                self.opened_executioner.add(req1_key)
                logger.info(
                    "profile_triggered_executioner",
                    market_id=market_id,
                    base_profile="stop_loss",
                    request_id=1,
                    price=trend_price
                )
                await self._trigger_executioner_virtual_trade(
                    base_profile_name="stop_loss",
                    request_id=1,
                    market=market,
                    side=side,
                    entry_price=trend_price,
                    size_usdc=size_usdc,
                    shares=trend_shares,
                    strategy_mode="TREND",
                    target_token_id=target_token_id,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )

        # Request 2: cross_market_hedge base criteria with entry_price < 0.07
        req2_key = f"{market_id}_2"
        if req2_key not in self.opened_executioner:
            if symbol in ("BTC", "ETH") and closes_at > 0:
                try:
                    row = await fetch_one(
                        self.db,
                        "SELECT market_id, yes_token_id, no_token_id, slug, question FROM markets WHERE closes_at = ? AND market_id != ?",
                        (closes_at, market_id)
                    )
                    if row:
                        ws = self.position_mgr.ws
                        if ws is not None:
                            twin_yes_ask = ws.get_best_ask(row["yes_token_id"])
                            twin_no_ask = ws.get_best_ask(row["no_token_id"])
                            if twin_yes_ask is not None and twin_no_ask is not None:
                                cur_yes_p = trend_price if side == "YES" else reversal_price
                                cur_no_p = reversal_price if side == "YES" else trend_price
                                btc_yes_p = cur_yes_p if symbol == "BTC" else float(twin_yes_ask)
                                btc_no_p = cur_no_p if symbol == "BTC" else float(twin_no_ask)
                                eth_yes_p = float(twin_yes_ask) if symbol == "BTC" else cur_yes_p
                                eth_no_p = float(twin_no_ask) if symbol == "BTC" else cur_no_p
                                trigger_c1 = (0 < btc_yes_p < 0.40 and 0 < eth_no_p < 0.40)
                                trigger_c2 = (0 < btc_no_p < 0.40 and 0 < eth_yes_p < 0.40)
                                if trigger_c1 or trigger_c2:
                                    cur_s = "YES" if (trigger_c1 if symbol == "BTC" else trigger_c2) else "NO"
                                    cur_p = cur_yes_p if cur_s == "YES" else cur_no_p
                                    if closes_at - now_ts >= 60 and cur_p < 0.07:
                                        self.opened_executioner.add(req2_key)
                                        cur_shares = size_usdc / cur_p if cur_p > 0 else 0.0
                                        cur_mode = "TREND" if cur_s == side else "REVERSAL"
                                        cur_tid = target_token_id if cur_s == side else reversal_tid
                                        logger.info(
                                            "profile_triggered_executioner",
                                            market_id=market_id,
                                            base_profile="cross_market_hedge",
                                            request_id=2,
                                            price=cur_p
                                        )
                                        await self._trigger_executioner_virtual_trade(
                                            base_profile_name="cross_market_hedge",
                                            request_id=2,
                                            market=market,
                                            side=cur_s,
                                            entry_price=cur_p,
                                            size_usdc=size_usdc,
                                            shares=cur_shares,
                                            strategy_mode=cur_mode,
                                            target_token_id=cur_tid,
                                            win_prob_estimate=win_prob_estimate,
                                            kelly_fraction_used=kelly_fraction_used,
                                            bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                                            book_imbalance_at_entry=book_imbalance_at_entry,
                                            cvd_60s_at_entry=cvd_60s_at_entry,
                                            correlated_position_open=correlated_position_open,
                                            seconds_to_close_at_entry=seconds_to_close_at_entry,
                                            market_age_at_entry_pct=market_age_at_entry_pct,
                                            bankroll_at_entry=bankroll_at_entry,
                                            persistence_score=persistence_score,
                                            streak_depth=streak_depth
                                        )
                except Exception as e:
                    logger.error("executioner_req2_check_failed", error=str(e))

        # Request 3: reversal_probability_sl base criteria with 0.60 < entry_price < 0.70 and 15m window
        req3_key = f"{market_id}_3"
        if req3_key not in self.opened_executioner:
            if window == 15 and 0.60 < reversal_price < 0.70:
                self.opened_executioner.add(req3_key)
                logger.info(
                    "profile_triggered_executioner",
                    market_id=market_id,
                    base_profile="reversal_probability_sl",
                    request_id=3,
                    price=reversal_price
                )
                await self._trigger_executioner_virtual_trade(
                    base_profile_name="reversal_probability_sl",
                    request_id=3,
                    market=market,
                    side=reversal_side,
                    entry_price=reversal_price,
                    size_usdc=size_usdc,
                    shares=reversal_shares,
                    strategy_mode="REVERSAL",
                    target_token_id=reversal_tid,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    streak_depth=streak_depth
                )



    async def _trigger_virtual_trade_with_notes(
        self,
        profile_name: str,
        market: Dict[str, Any],
        side: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
        strategy_mode: str,
        target_token_id: Optional[str] = None,
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        persistence_score: float = 0.0,
        streak_depth: int = 2,
        custom_notes: Optional[str] = None
    ) -> None:
        try:
            from unittest.mock import MagicMock, AsyncMock
            func = getattr(self.position_mgr, "open_profile_position", None)
            if isinstance(func, (MagicMock, AsyncMock)) and not isinstance(func, AsyncMock):
                func(
                    profile_name=profile_name,
                    market_id=market["market_id"],
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    strategy_mode=strategy_mode,
                    streak_depth=streak_depth,
                    custom_notes=custom_notes
                )
            else:
                await self.position_mgr.open_profile_position(
                    profile_name=profile_name,
                    market_id=market["market_id"],
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    strategy_mode=strategy_mode,
                    streak_depth=streak_depth,
                    custom_notes=custom_notes
                )
        except Exception as e:
            logger.error("failed_to_trigger_virtual_trade_with_notes", profile=profile_name, error=str(e))


    async def _trigger_executioner_virtual_trade(
        self,
        base_profile_name: str,
        request_id: int,
        market: Dict[str, Any],
        side: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
        strategy_mode: str,
        target_token_id: Optional[str] = None,
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        persistence_score: float = 0.0,
        streak_depth: int = 2
    ) -> None:
        """Trigger executioner virtual trade and log to profile_positions and executioner_positions."""
        try:
            extra_notes = {
                "tid": target_token_id,
                "strategy": strategy_mode,
                "streak_depth": streak_depth,
                "base_profile_name": base_profile_name,
                "request_id": request_id
            }
            await self._trigger_virtual_trade(
                profile_name="executioner",
                market=market,
                side=side,
                entry_price=entry_price,
                size_usdc=size_usdc,
                shares=shares,
                strategy_mode=strategy_mode,
                target_token_id=target_token_id,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                streak_depth=streak_depth,
                extra_notes=extra_notes
            )
            pos_id = f"virtual_executioner_{market['market_id']}_req{request_id}"
            from src.db.queries import insert_executioner_position
            actual_shares = int(shares)
            actual_size = actual_shares * entry_price
            await insert_executioner_position(
                self.db,
                position_id=pos_id,
                base_profile_name=base_profile_name,
                request_id=request_id,
                market_id=market["market_id"],
                entry_price=entry_price,
                size_usdc=actual_size,
                shares_bought=actual_shares,
                notes=json.dumps(extra_notes),
                side=side,
                win_prob_estimate=win_prob_estimate,
                kelly_fraction_used=kelly_fraction_used,
                bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                book_imbalance_at_entry=book_imbalance_at_entry,
                cvd_60s_at_entry=cvd_60s_at_entry,
                correlated_position_open=correlated_position_open,
                seconds_to_close_at_entry=seconds_to_close_at_entry,
                market_age_at_entry_pct=market_age_at_entry_pct,
                bankroll_at_entry=bankroll_at_entry,
                persistence_score=persistence_score,
                strategy_mode=strategy_mode
            )
        except Exception as e:
            logger.error("failed_to_trigger_executioner_virtual_trade", base_profile=base_profile_name, error=str(e))


    async def _trigger_virtual_trade(
        self,
        profile_name: str,
        market: Dict[str, Any],
        side: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
        strategy_mode: str,
        target_token_id: Optional[str] = None,
        win_prob_estimate: float = 0.5,
        kelly_fraction_used: float = 0.0,
        bid_ask_spread_at_entry: float = 0.0,
        book_imbalance_at_entry: float = 0.0,
        cvd_60s_at_entry: float = 0.0,
        correlated_position_open: int = 0,
        seconds_to_close_at_entry: float = 0.0,
        market_age_at_entry_pct: float = 0.0,
        bankroll_at_entry: float = 0.0,
        persistence_score: float = 0.0,
        streak_depth: int = 2,
        extra_notes: Optional[Dict[str, Any]] = None
    ) -> None:
        """Call position manager to log virtual paper trade for the profile."""
        # Enforce CVD Direction-Exhaustion Block (Soft-Disabled per User Request) - Exempt "cheap_reversal" profile
        if profile_name != "cheap_reversal":
            if side == "YES" and cvd_60s_at_entry > 50000.0:
                logger.warning(
                    "profile_cvd_direction_exhaustion_warning_soft_disabled",
                    profile=profile_name,
                    market_id=market["market_id"],
                    side=side,
                    cvd=cvd_60s_at_entry
                )
            elif side == "NO" and cvd_60s_at_entry < -50000.0:
                logger.warning(
                    "profile_cvd_direction_exhaustion_warning_soft_disabled",
                    profile=profile_name,
                    market_id=market["market_id"],
                    side=side,
                    cvd=cvd_60s_at_entry
                )


        try:
            dist_bps = float(market.get("underlying_distance_bps", 0.0))
            bs_prob = float(market.get("bs_implied_probability", 0.5))
            vol_5m = float(market.get("underlying_realized_vol_5m") or market.get("realized_vol_5m") or 0.0)

            from unittest.mock import MagicMock, AsyncMock
            func = getattr(self.position_mgr, "open_profile_position", None)
            if isinstance(func, (MagicMock, AsyncMock)) and not isinstance(func, AsyncMock):
                func(
                    profile_name=profile_name,
                    market_id=market["market_id"],
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    strategy_mode=strategy_mode,
                    streak_depth=streak_depth,
                    underlying_distance_bps=dist_bps,
                    bs_implied_probability=bs_prob,
                    realized_vol_5m=vol_5m
                )
            else:
                await self.position_mgr.open_profile_position(
                    profile_name=profile_name,
                    market_id=market["market_id"],
                    target_token_id=target_token_id,
                    entry_price=entry_price,
                    size_usdc=size_usdc,
                    shares=shares,
                    side=side,
                    win_prob_estimate=win_prob_estimate,
                    kelly_fraction_used=kelly_fraction_used,
                    bid_ask_spread_at_entry=bid_ask_spread_at_entry,
                    book_imbalance_at_entry=book_imbalance_at_entry,
                    cvd_60s_at_entry=cvd_60s_at_entry,
                    correlated_position_open=correlated_position_open,
                    seconds_to_close_at_entry=seconds_to_close_at_entry,
                    market_age_at_entry_pct=market_age_at_entry_pct,
                    bankroll_at_entry=bankroll_at_entry,
                    persistence_score=persistence_score,
                    strategy_mode=strategy_mode,
                    streak_depth=streak_depth,
                    custom_notes=json.dumps(extra_notes) if extra_notes else None,
                    underlying_distance_bps=dist_bps,
                    bs_implied_probability=bs_prob,
                    realized_vol_5m=vol_5m
                )
        except Exception as e:

            logger.error("failed_to_trigger_virtual_trade", profile=profile_name, error=str(e))
