"""
Bot-side State Exporter — Serializes full market state to shared memory file every 2 seconds.
"""

import json
import os
import time
from typing import Any, Dict

from src.logs.log_config import get_logger
from src.recorder.config import (
    SHM_TMP_PATH,
    SHM_FINAL_PATH,
    FALLBACK_TMP_PATH,
    FALLBACK_FINAL_PATH,
    RECORDED_SYMBOLS,
)

logger = get_logger("state_exporter")

class StateExporter:
    def __init__(self, config=None):
        self.config = config
        self._tmp_path, self._final_path = self._determine_paths()

    def _determine_paths(self):
        """Check if /dev/shm is available and writable, otherwise use local fallback."""
        try:
            shm_dir = "/dev/shm"
            if os.path.exists(shm_dir) and os.access(shm_dir, os.W_OK):
                return SHM_TMP_PATH, SHM_FINAL_PATH
        except Exception:
            pass
        
        fallback_dir = os.path.dirname(FALLBACK_TMP_PATH)
        os.makedirs(fallback_dir, exist_ok=True)
        return FALLBACK_TMP_PATH, FALLBACK_FINAL_PATH

    def _clean_val(self, val, default=0.0):
        if val is None:
            return default
        if type(val).__name__ in ('MagicMock', 'AsyncMock', 'Mock'):
            return default
        try:
            if isinstance(val, (int, float, str, bool)):
                return val
            return float(val)
        except Exception:
            return default

    async def export_state_snapshot(self, scanner, position_mgr=None, binance_ws=None):
        """
        Extract live snapshot from scanner and write atomically to shared memory.
        Fully isolated: any error is logged and swallowed so bot loop is never affected.
        """
        try:
            now_ts = time.time()
            now_ms = int(now_ts * 1000)
            bot_version = str(self._clean_val(getattr(scanner.config, "BOT_VERSION", "v4.50"), "v4.50"))

            snapshot = {
                "timestamp_ms": now_ms,
                "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
                "bot_version": bot_version,
                "markets": {},
                "external": {},
                "positions": {},
            }

            # 1. Market Snapshots
            tracked = getattr(scanner, "tracked_markets", {})
            ws = getattr(scanner, "ws", None)
            
            for mid, mkt in list(tracked.items()):
                yes_tid = mkt.get("yes_token_id")
                no_tid = mkt.get("no_token_id")
                symbol = mkt.get("symbol", "BTC")
                window = mkt.get("window_minutes", 5)
                closes_at = mkt.get("closes_at", 0)

                # Prices from WS cache
                yes_price = ws.price_cache.get(yes_tid) if ws and yes_tid else mkt.get("yes_price")
                no_price = ws.price_cache.get(no_tid) if ws and no_tid else mkt.get("no_price")
                yes_best_bid = ws.bid_cache.get(yes_tid) if ws and yes_tid else None
                yes_best_ask = ws.ask_cache.get(yes_tid) if ws and yes_tid else None
                no_best_bid = ws.bid_cache.get(no_tid) if ws and no_tid else None
                no_best_ask = ws.ask_cache.get(no_tid) if ws and no_tid else None

                # Compute bid-ask spread
                spread = None
                if yes_best_ask is not None and yes_best_bid is not None:
                    spread = round(yes_best_ask - yes_best_bid, 4)

                # Orderbook depth & imbalance
                book = ws.book_cache.get(yes_tid) if ws and yes_tid else None
                depth_1c, depth_5c, ask_depth_1c, bid_depth_1c = 0.0, 0.0, 0.0, 0.0
                imbalance = 0.0
                if book:
                    best_ask_val = yes_best_ask or 0.50
                    best_bid_val = yes_best_bid or 0.50
                    if hasattr(scanner, "_compute_orderbook_depths"):
                        depth_1c, depth_5c, ask_depth_1c, bid_depth_1c = scanner._compute_orderbook_depths(book, best_ask_val, best_bid_val)
                    if hasattr(scanner, "_compute_orderbook_imbalance"):
                        imbalance = scanner._compute_orderbook_imbalance(book)

                # Underlying metrics
                spot_price = 0.0
                velocity_10s = 0.0
                realized_vol_5m = 0.0
                if hasattr(scanner, "_spot_ticks") and symbol in scanner._spot_ticks and scanner._spot_ticks[symbol]:
                    spot_price = scanner._spot_ticks[symbol][-1][1]
                if hasattr(scanner, "_get_spot_velocity_10s"):
                    velocity_10s = scanner._get_spot_velocity_10s(symbol)
                if hasattr(scanner, "_get_realized_volatility"):
                    realized_vol_5m = scanner._get_realized_volatility(symbol, 300)

                # CVD from Binance
                cvd_60s = 0.0
                cvd_5m = 0.0
                if binance_ws:
                    if hasattr(binance_ws, "get_cvd"):
                        cvd_60s = binance_ws.get_cvd(symbol, 60)
                        cvd_5m = binance_ws.get_cvd(symbol, 300)
                    elif hasattr(binance_ws, "cvd") and symbol in binance_ws.cvd:
                        cvd_60s = binance_ws.cvd.get(symbol, 0.0)

                secs_remaining = max(0.0, closes_at - now_ts) if closes_at > 0 else 0.0

                snapshot["markets"][mid] = {
                    "market_id": str(mid),
                    "symbol": str(symbol),
                    "window_minutes": int(self._clean_val(window, 5)),
                    "closes_at": int(self._clean_val(closes_at, 0)),
                    "yes_price": self._clean_val(yes_price, 0.0),
                    "no_price": self._clean_val(no_price, 0.0),
                    "yes_best_bid": self._clean_val(yes_best_bid, None),
                    "yes_best_ask": self._clean_val(yes_best_ask, None),
                    "no_best_bid": self._clean_val(no_best_bid, None),
                    "no_best_ask": self._clean_val(no_best_ask, None),
                    "bid_ask_spread": self._clean_val(spread, None),
                    "book_imbalance": self._clean_val(imbalance, 0.0),
                    "depth_usd_1c": self._clean_val(depth_1c, 0.0),
                    "depth_usd_5c": self._clean_val(depth_5c, 0.0),
                    "ask_depth_usd_1c": self._clean_val(ask_depth_1c, 0.0),
                    "bid_depth_usd_1c": self._clean_val(bid_depth_1c, 0.0),
                    "cvd_60s": self._clean_val(cvd_60s, 0.0),
                    "cvd_5m": self._clean_val(cvd_5m, 0.0),
                    "streak_depth": int(self._clean_val(mkt.get("streak_depth"), 0)),
                    "streak_quality": self._clean_val(mkt.get("streak_quality"), 50.0),
                    "persistence_score": self._clean_val(mkt.get("persistence_score"), 0.50),
                    "strategy_mode": str(mkt.get("active_strategy", "TREND")),
                    "spot_price": self._clean_val(spot_price, 0.0),
                    "spot_velocity_10s": self._clean_val(velocity_10s, 0.0),
                    "realized_vol_5m": self._clean_val(realized_vol_5m, 0.0),
                    "seconds_remaining": self._clean_val(secs_remaining, 0.0),
                }

            # 2. External Binance Ticks (BTC, ETH, SOL, XRP)
            for sym in RECORDED_SYMBOLS:
                spot_price = 0.0
                if hasattr(scanner, "_spot_ticks") and sym in scanner._spot_ticks and scanner._spot_ticks[sym]:
                    spot_price = scanner._spot_ticks[sym][-1][1]
                elif binance_ws and hasattr(binance_ws, "_last_price"):
                    spot_price = binance_ws._last_price.get(sym, 0.0)

                cvd_60s = binance_ws.get_cvd(sym, 60) if binance_ws and hasattr(binance_ws, "get_cvd") else 0.0
                cvd_5m = binance_ws.get_cvd(sym, 300) if binance_ws and hasattr(binance_ws, "get_cvd") else 0.0
                velocity_10s = scanner._get_spot_velocity_10s(sym) if hasattr(scanner, "_get_spot_velocity_10s") else 0.0
                realized_vol_5m = scanner._get_realized_volatility(sym, 300) if hasattr(scanner, "_get_realized_volatility") else 0.0
                realized_vol_1h = scanner._get_realized_volatility(sym, 3600) if hasattr(scanner, "_get_realized_volatility") else 0.0

                snapshot["external"][sym] = {
                    "symbol": str(sym),
                    "spot_price": self._clean_val(spot_price, 0.0),
                    "cvd_60s": self._clean_val(cvd_60s, 0.0),
                    "cvd_5m": self._clean_val(cvd_5m, 0.0),
                    "spot_velocity_10s": self._clean_val(velocity_10s, 0.0),
                    "realized_vol_5m": self._clean_val(realized_vol_5m, 0.0),
                    "realized_vol_1h": self._clean_val(realized_vol_1h, 0.0),
                }

            # Write atomically via temp file
            with open(self._tmp_path, "w") as f:
                json.dump(snapshot, f)
            os.rename(self._tmp_path, self._final_path)

        except Exception as e:
            logger.error("export_state_snapshot_failed", error=str(e))
