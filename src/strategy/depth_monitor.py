"""
Depth Monitor — Orderbook depth snapshot recorder for overnight analysis.

PURELY OBSERVATIONAL: Records orderbook depth data and simulates 3 fill
strategies (Chunked FAK Sweep, Partial Fill, Deferred Retry) WITHOUT
modifying any trading execution.

v3.56
"""

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.logs.log_config import get_logger

logger = get_logger("depth_monitor")


CREATE_DEPTH_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS depth_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    side TEXT NOT NULL,
    requested_shares REAL NOT NULL,
    entry_price REAL NOT NULL,
    streak_depth INTEGER DEFAULT 2,
    book_asks_json TEXT,
    book_bids_json TEXT,
    total_ask_depth_shares REAL DEFAULT 0,
    total_bid_depth_shares REAL DEFAULT 0,
    chunked_n_levels INTEGER DEFAULT 0,
    chunked_fillable_shares REAL DEFAULT 0,
    chunked_avg_price REAL DEFAULT 0,
    chunked_max_price REAL DEFAULT 0,
    chunked_full_fill INTEGER DEFAULT 0,
    chunked_detail_json TEXT,
    partial_filled_shares REAL DEFAULT 0,
    partial_filled_pct REAL DEFAULT 0,
    partial_avg_price REAL DEFAULT 0,
    partial_recovery_pct REAL DEFAULT 0,
    deferred_delay_ms INTEGER DEFAULT 500,
    deferred_book_asks_json TEXT,
    deferred_total_ask_depth REAL DEFAULT 0,
    deferred_fillable_shares REAL DEFAULT 0,
    deferred_avg_price REAL DEFAULT 0,
    deferred_full_fill INTEGER DEFAULT 0,
    deferred_book_changed INTEGER DEFAULT 0,
    deferred_detail_json TEXT,
    window_minutes INTEGER DEFAULT 5,
    symbol TEXT,
    is_reversal INTEGER DEFAULT 0
);
"""


def _simulate_chunked_sweep(
    asks: Dict[str, str], requested_shares: float
) -> Dict[str, Any]:
    """
    Strategy 1 — Chunked FAK Sweep.

    Walk the ask side level by level, filling chunks until the full
    share count is reached or the book runs out.

    Args:
        asks: Dict of {price_str: size_str} from the WS book cache.
        requested_shares: Number of shares to fill.

    Returns:
        Dict with sweep simulation results.
    """
    if not asks or requested_shares <= 0:
        return {
            "n_levels": 0,
            "fillable_shares": 0.0,
            "avg_price": 0.0,
            "max_price": 0.0,
            "full_fill": False,
            "levels": [],
        }

    # Sort ask levels by price ascending (cheapest first)
    sorted_levels = sorted(
        [(float(p), float(s)) for p, s in asks.items()],
        key=lambda x: x[0],
    )

    filled = 0.0
    cost = 0.0
    n_levels = 0
    max_price = 0.0
    level_details = []

    for price, size in sorted_levels:
        if filled >= requested_shares:
            break

        remaining = requested_shares - filled
        take = min(size, remaining)
        filled += take
        cost += take * price
        n_levels += 1
        max_price = price
        level_details.append({
            "price": price,
            "size_available": size,
            "filled": take,
        })

    avg_price = (cost / filled) if filled > 0 else 0.0
    full_fill = filled >= requested_shares

    return {
        "n_levels": n_levels,
        "fillable_shares": round(filled, 4),
        "avg_price": round(avg_price, 6),
        "max_price": round(max_price, 6),
        "full_fill": full_fill,
        "levels": level_details,
    }


def _simulate_partial_fill(
    sweep_result: Dict[str, Any],
    requested_shares: float,
    entry_price: float,
) -> Dict[str, Any]:
    """
    Strategy 2 — Partial Fill.

    If sweep can't fill everything, what % can be filled?  What's the
    projected profit recovery if that partial position wins ($1.00 payout)?

    Returns:
        Dict with partial fill simulation results.
    """
    fillable = sweep_result["fillable_shares"]
    avg_price = sweep_result["avg_price"]

    if requested_shares <= 0:
        return {
            "filled_shares": 0.0,
            "filled_pct": 0.0,
            "avg_price": 0.0,
            "recovery_pct": 0.0,
        }

    filled_pct = (fillable / requested_shares) * 100.0

    # Recovery % = projected profit from partial fill vs full fill
    # Full fill profit: requested_shares * (1.0 - entry_price)
    # Partial fill profit: fillable * (1.0 - avg_price)
    full_profit = requested_shares * (1.0 - entry_price)
    partial_profit = fillable * (1.0 - avg_price) if avg_price > 0 else 0.0
    recovery_pct = (partial_profit / full_profit * 100.0) if full_profit > 0 else 0.0

    return {
        "filled_shares": round(fillable, 4),
        "filled_pct": round(filled_pct, 2),
        "avg_price": round(avg_price, 6),
        "recovery_pct": round(recovery_pct, 2),
    }


def _total_depth(book_side: Dict[str, str]) -> float:
    """Sum total shares available on one side of the book."""
    if not book_side:
        return 0.0
    return sum(float(s) for s in book_side.values())


class DepthMonitor:
    """
    Records orderbook depth snapshots and simulates fill strategies.
    Purely observational — does NOT modify trading execution.
    """

    def __init__(self, db, ws_client=None):
        self.db = db
        self.ws = ws_client

    async def ensure_table(self):
        """Create the depth_snapshots table if it doesn't exist."""
        await self.db.execute(CREATE_DEPTH_SNAPSHOTS_SQL)
        await self.db.commit()

    async def record_depth_snapshot(
        self,
        market_id: str,
        token_id: str,
        side: str,
        requested_shares: float,
        entry_price: float,
        streak_depth: int = 2,
        window_minutes: int = 5,
        symbol: str = "BTC",
        is_reversal: bool = False,
    ) -> Optional[str]:
        """
        Snapshot the current orderbook depth and simulate 3 fill strategies.

        Runs as a fire-and-forget background task — the 500ms deferred
        retry delay does NOT block actual order placement.

        Returns:
            snapshot_id on success, None on error.
        """
        try:
            snapshot_id = str(uuid.uuid4())
            ts = int(time.time())

            # ─── Snapshot 1: Current book state ───
            book = {}
            if self.ws and hasattr(self.ws, "book_cache"):
                book = self.ws.book_cache.get(token_id, {})

            asks = book.get("asks", {})
            bids = book.get("bids", {})

            total_ask_depth = _total_depth(asks)
            total_bid_depth = _total_depth(bids)

            # ─── Strategy 1: Chunked FAK Sweep ───
            sweep = _simulate_chunked_sweep(asks, requested_shares)

            # ─── Strategy 2: Partial Fill ───
            partial = _simulate_partial_fill(sweep, requested_shares, entry_price)

            # ─── Strategy 3: Deferred Retry (500ms delay) ───
            await asyncio.sleep(0.5)

            deferred_book = {}
            if self.ws and hasattr(self.ws, "book_cache"):
                deferred_book = self.ws.book_cache.get(token_id, {})

            deferred_asks = deferred_book.get("asks", {})
            deferred_total_ask_depth = _total_depth(deferred_asks)
            deferred_sweep = _simulate_chunked_sweep(deferred_asks, requested_shares)
            deferred_book_changed = 1 if deferred_asks != asks else 0

            # ─── Store to DB ───
            await self.db.execute(
                """
                INSERT INTO depth_snapshots (
                    snapshot_id, market_id, token_id, timestamp, side,
                    requested_shares, entry_price, streak_depth,
                    book_asks_json, book_bids_json,
                    total_ask_depth_shares, total_bid_depth_shares,
                    chunked_n_levels, chunked_fillable_shares,
                    chunked_avg_price, chunked_max_price,
                    chunked_full_fill, chunked_detail_json,
                    partial_filled_shares, partial_filled_pct,
                    partial_avg_price, partial_recovery_pct,
                    deferred_delay_ms, deferred_book_asks_json,
                    deferred_total_ask_depth, deferred_fillable_shares,
                    deferred_avg_price, deferred_full_fill,
                    deferred_book_changed, deferred_detail_json,
                    window_minutes, symbol, is_reversal
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    snapshot_id, market_id, token_id, ts, side,
                    requested_shares, entry_price, streak_depth,
                    json.dumps(asks), json.dumps(bids),
                    total_ask_depth, total_bid_depth,
                    sweep["n_levels"], sweep["fillable_shares"],
                    sweep["avg_price"], sweep["max_price"],
                    1 if sweep["full_fill"] else 0, json.dumps(sweep["levels"]),
                    partial["filled_shares"], partial["filled_pct"],
                    partial["avg_price"], partial["recovery_pct"],
                    500, json.dumps(deferred_asks),
                    deferred_total_ask_depth, deferred_sweep["fillable_shares"],
                    deferred_sweep["avg_price"], 1 if deferred_sweep["full_fill"] else 0,
                    deferred_book_changed, json.dumps(deferred_sweep["levels"]),
                    window_minutes, symbol, 1 if is_reversal else 0,
                ),
            )
            await self.db.commit()

            logger.info(
                "depth_snapshot_recorded",
                snapshot_id=snapshot_id,
                market_id=market_id,
                side=side,
                requested_shares=requested_shares,
                total_ask_depth=round(total_ask_depth, 2),
                chunked_full_fill=sweep["full_fill"],
                chunked_levels=sweep["n_levels"],
                chunked_avg_price=sweep["avg_price"],
                partial_pct=partial["filled_pct"],
                deferred_changed=bool(deferred_book_changed),
                deferred_full_fill=deferred_sweep["full_fill"],
            )

            return snapshot_id

        except Exception as e:
            logger.error("depth_snapshot_failed", error=str(e), market_id=market_id)
            return None
