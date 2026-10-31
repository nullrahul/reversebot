import json
import sqlite3
import os
import time
import asyncio
from typing import Dict, Any
from .weights import RouterWeightsLoader
from .scorer import RouterScorer
from .policy import RouterPolicy
from src.logs.log_config import get_logger

logger = get_logger("shadow_router_logger")

class ShadowRouterLogger:
    def __init__(self, db_path: str = None, weights_loader: RouterWeightsLoader = None):
        if db_path is None:
            db_path = os.path.expanduser('~/reversebot/data/reversebot_rd.db')
            if not os.path.exists(db_path):
                db_path = 'data/reversebot_rd.db'
        self.db_path = os.path.abspath(db_path)
        self.weights = weights_loader or RouterWeightsLoader()
        self.scorer = RouterScorer(self.weights)
        self.policy = RouterPolicy(self.weights)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout = 60000")
        return conn

    async def evaluate_and_log(self, signal_payload: Dict[str, Any], legacy_decision: str) -> Dict[str, Any]:
        """
        Non-blocking async evaluation and database log of challenger decision.
        Safeguarded so ANY exception inside the shadow logger NEVER crashes legacy execution.
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._sync_evaluate_and_log, signal_payload, legacy_decision)
        except Exception as e:
            logger.error("shadow_router_async_evaluate_failed", error=str(e))
            return {
                "router_decision": "ERROR",
                "reason_rejected": f"LoggerException: {e}",
                "winning_engine": "None",
                "winning_score": 0.0,
                "score_margin": 0.0
            }

    def _sync_evaluate_and_log(self, signal_payload: Dict[str, Any], legacy_decision: str) -> Dict[str, Any]:
        t0 = time.perf_counter()

        try:
            profile_name = str(signal_payload.get("profile_name", ""))
            
            # Profile Allowlist Guard: Ignore signals from non-allowlisted profiles (e.g. loss_skip, telemetry, raw baselines)
            if profile_name and not self.weights.is_profile_allowed(profile_name):
                logger.info("shadow_router_signal_skipped_not_on_allowlist", profile=profile_name)
                return {
                    "router_decision": "REJECT",
                    "reason_rejected": f"NotOnAllowlist: {profile_name}",
                    "winning_engine": "None",
                    "winning_score": 0.0,
                    "score_margin": 0.0
                }

            scores = self.scorer.calculate_scores(signal_payload)
            router_decision, reason_rejected, winning_score, second_best_score, score_margin = self.policy.evaluate_policy(scores)

            market_id = str(signal_payload.get("market_id", ""))
            signal_id = str(signal_payload.get("signal_id", ""))
            asset = str(signal_payload.get("asset", "BTC"))
            entry_price = float(signal_payload.get("entry_price", 0.50))
            cvd_60s = float(signal_payload.get("cvd_60s", 0.0))
            book_imbalance = float(signal_payload.get("book_imbalance", 0.0))
            streak_depth = int(signal_payload.get("streak_depth", 2))
            seconds_to_close = float(signal_payload.get("seconds_to_close", 300.0))

            winning_engine = sorted(scores.items(), key=lambda x: x[1], reverse=True)[0][0] if scores else "None"
            market_regime = "Discount" if entry_price < 0.45 else ("Premium" if entry_price > 0.60 else "MidPrice")

            eval_ms = round((time.perf_counter() - t0) * 1000.0, 3)

            # Database Insertion into shadow_router_logs
            conn = self._get_conn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO shadow_router_logs (
                        signal_id, market_id, router_version, weights_version, feature_schema_version,
                        entry_price, cvd_60s, book_imbalance, streak_depth, seconds_to_close, asset,
                        legacy_decision, router_decision, winning_engine, winning_score, second_best_score,
                        score_margin, reason_rejected, market_regime, conviction_level, raw_scores_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal_id, market_id, self.weights.router_version, self.weights.weights_version, self.weights.feature_schema_version,
                    entry_price, cvd_60s, book_imbalance, streak_depth, seconds_to_close, asset,
                    legacy_decision, router_decision, winning_engine, winning_score, second_best_score,
                    score_margin, reason_rejected, market_regime, f"Score_{winning_score:.2f}", json.dumps(scores)
                ))
                conn.commit()
            finally:
                conn.close()

            logger.info(
                "shadow_router_decision_logged",
                market_id=market_id,
                router_decision=router_decision,
                winning_engine=winning_engine,
                winning_score=winning_score,
                score_margin=score_margin,
                eval_ms=eval_ms
            )

            return {
                "router_decision": router_decision,
                "winning_engine": winning_engine,
                "winning_score": winning_score,
                "score_margin": score_margin,
                "reason_rejected": reason_rejected,
                "scores": scores,
                "eval_ms": eval_ms
            }
        except Exception as sync_err:
            logger.error("shadow_router_sync_log_failed", error=str(sync_err), market_id=signal_payload.get("market_id"))
            return {
                "router_decision": "ERROR",
                "reason_rejected": f"SyncLogException: {sync_err}",
                "winning_engine": "None",
                "winning_score": 0.0,
                "score_margin": 0.0
            }

    def reconcile_shadow_outcomes(self) -> int:
        """
        Decoupled reconciliation job: Matches shadow logs with resolved profile positions
        and computes realized_router_pnl & delta_pnl post-resolution.
        """
        try:
            conn = self._get_conn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE shadow_router_logs
                    SET 
                        resolved_at = (SELECT closed_at FROM profile_positions p WHERE p.market_id = shadow_router_logs.market_id AND p.realised_pnl IS NOT NULL LIMIT 1),
                        legacy_pnl = (SELECT SUM(realised_pnl) FROM profile_positions p WHERE p.market_id = shadow_router_logs.market_id AND p.realised_pnl IS NOT NULL),
                        realized_router_pnl = CASE 
                            WHEN router_decision = 'ACCEPT' THEN (SELECT SUM(realised_pnl) FROM profile_positions p WHERE p.market_id = shadow_router_logs.market_id AND p.realised_pnl IS NOT NULL)
                            ELSE 0.0
                        END,
                        delta_pnl = CASE 
                            WHEN router_decision = 'ACCEPT' THEN 0.0
                            ELSE -1.0 * IFNULL((SELECT SUM(realised_pnl) FROM profile_positions p WHERE p.market_id = shadow_router_logs.market_id AND p.realised_pnl IS NOT NULL), 0.0)
                        END
                    WHERE resolved_at IS NULL
                      AND EXISTS (SELECT 1 FROM profile_positions p WHERE p.market_id = shadow_router_logs.market_id AND p.realised_pnl IS NOT NULL)
                """)
                updated_count = cur.rowcount
                conn.commit()
                return updated_count
            finally:
                conn.close()
        except Exception as rec_err:
            logger.error("failed_to_reconcile_shadow_outcomes", error=str(rec_err))
            return 0
