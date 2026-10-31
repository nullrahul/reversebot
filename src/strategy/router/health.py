"""
Router Daily Health Telemetry & Anomaly Detector
Computes daily feature and filter distribution stats comparing Today vs 7-day Rolling Averages.
"""
import sqlite3
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def get_router_daily_health(db_path: str) -> Dict[str, Any]:
    """
    Queries shadow_router_logs table to generate the daily health report.
    Returns metric stats comparing Today vs 7-Day Moving Averages.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=60.0)
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Check if shadow_router_logs table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_router_logs'")
        if not cursor.fetchone():
            conn.close()
            return {"status": "INITIALIZING", "message": "shadow_router_logs table not yet populated"}

        # Query metrics for Today (last 24 hours) vs 7-Day Average
        query_today = """
            SELECT 
                COUNT(*) as total_signals,
                SUM(CASE WHEN router_decision = 'ACCEPT' THEN 1 ELSE 0 END) as accepted_cnt,
                SUM(CASE WHEN rejection_reason LIKE '%GrayZone%' THEN 1 ELSE 0 END) as gray_zone_cnt,
                SUM(CASE WHEN rejection_reason LIKE '%ConflictedRegime%' THEN 1 ELSE 0 END) as tie_cnt,
                SUM(CASE WHEN winning_engine = 'Reversal' THEN 1 ELSE 0 END) as rev_cnt,
                SUM(CASE WHEN winning_engine = 'Micro_Flow' THEN 1 ELSE 0 END) as flow_cnt,
                AVG(eval_latency_ms) as avg_latency_ms
            FROM shadow_router_logs
            WHERE evaluated_at >= datetime('now', '-1 day')
        """
        cursor.execute(query_today)
        row_today = dict(cursor.fetchone() or {})

        query_7d = """
            SELECT 
                COUNT(*) / 7.0 as total_signals,
                SUM(CASE WHEN router_decision = 'ACCEPT' THEN 1 ELSE 0 END) / 7.0 as accepted_cnt,
                SUM(CASE WHEN rejection_reason LIKE '%GrayZone%' THEN 1 ELSE 0 END) / 7.0 as gray_zone_cnt,
                SUM(CASE WHEN rejection_reason LIKE '%ConflictedRegime%' THEN 1 ELSE 0 END) / 7.0 as tie_cnt,
                SUM(CASE WHEN winning_engine = 'Reversal' THEN 1 ELSE 0 END) / 7.0 as rev_cnt,
                SUM(CASE WHEN winning_engine = 'Micro_Flow' THEN 1 ELSE 0 END) / 7.0 as flow_cnt,
                AVG(eval_latency_ms) as avg_latency_ms
            FROM shadow_router_logs
            WHERE evaluated_at >= datetime('now', '-7 days')
        """
        cursor.execute(query_7d)
        row_7d = dict(cursor.fetchone() or {})
        conn.close()

        tot_today = max(1, row_today.get("total_signals") or 0)
        tot_7d = max(1, row_7d.get("total_signals") or 1)

        metrics = {
            "total_signals": {"today": tot_today, "7d_avg": round(tot_7d, 1), "status": "🟢"},
            "accept_pct": {"today": round((row_today.get("accepted_cnt") or 0) / tot_today * 100, 1),
                           "7d_avg": round((row_7d.get("accepted_cnt") or 0) / tot_7d * 100, 1), "status": "🟢"},
            "gray_zone_pct": {"today": round((row_today.get("gray_zone_cnt") or 0) / tot_today * 100, 1),
                             "7d_avg": round((row_7d.get("gray_zone_cnt") or 0) / tot_7d * 100, 1), "status": "🟢"},
            "tie_pct": {"today": round((row_today.get("tie_cnt") or 0) / tot_today * 100, 1),
                        "7d_avg": round((row_7d.get("tie_cnt") or 0) / tot_7d * 100, 1), "status": "🟢"},
            "reversal_engine_pct": {"today": round((row_today.get("rev_cnt") or 0) / tot_today * 100, 1),
                                   "7d_avg": round((row_7d.get("rev_cnt") or 0) / tot_7d * 100, 1), "status": "🟢"},
            "micro_flow_engine_pct": {"today": round((row_today.get("flow_cnt") or 0) / tot_today * 100, 1),
                                      "7d_avg": round((row_7d.get("flow_cnt") or 0) / tot_7d * 100, 1), "status": "🟢"},
            "avg_latency_ms": {"today": round(row_today.get("avg_latency_ms") or 0.0, 3),
                               "7d_avg": round(row_7d.get("avg_latency_ms") or 0.0, 3), "status": "🟢"}
        }

        # Anomaly Detection: Flag 🟡 if Gray Zone spikes > 20% shift, or 🔴 if total signals drop by >50%
        if metrics["gray_zone_pct"]["today"] - metrics["gray_zone_pct"]["7d_avg"] > 20.0:
            metrics["gray_zone_pct"]["status"] = "🟡 (Distribution Shift)"
        if metrics["total_signals"]["today"] < metrics["total_signals"]["7d_avg"] * 0.5:
            metrics["total_signals"]["status"] = "🔴 (Low Feed Volume)"

        return {"status": "HEALTHY", "metrics": metrics}
    except Exception as e:
        logger.error("Failed to generate router daily health report", exc_info=True)
        return {"status": "ERROR", "message": str(e)}
