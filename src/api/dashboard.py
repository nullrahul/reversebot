"""
Web Dashboard API — Polymarket Reverse Bot.
FastAPI server exposing real-time stats, positions, history, and runtime parameter control.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import starlette.websockets
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from src.logs.log_config import DASHBOARD_CALLBACK
import src.logs.log_config as log_config
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from starlette.status import HTTP_403_FORBIDDEN
from src.logs.log_config import get_logger

logger = get_logger("dashboard")

# ─────────────────────────────────────────────
# State (Injected by main.py)
# ─────────────────────────────────────────────
_engine_ref = None
_kill_switch_ref = None
_config_ref = None

def set_engine(engine, kill_switch, config=None):
    global _engine_ref, _kill_switch_ref, _config_ref
    _engine_ref = engine
    _kill_switch_ref = kill_switch
    if config:
        _config_ref = config
    
    # Wire logs to web console
    log_config.DASHBOARD_CALLBACK = push_debug_event

# ─────────────────────────────────────────────
# Config Helpers (Uses the actual BotConfig object if available)
# ─────────────────────────────────────────────
def _get_db_path() -> str:
    if _config_ref:
        return _config_ref.db_path
    return os.getenv("DB_PATH", "data/reversebot.db")

def _is_paper() -> bool:
    if _config_ref:
        return _config_ref.paper_trade
    return os.getenv("PAPER_TRADE", "true").lower() == "true"

def _get_password() -> str:
    if _config_ref:
        return _config_ref.dashboard_password
    return os.getenv("DASHBOARD_PASSWORD", "reversebot2025")

def _get_runtime_config_path() -> Path:
    if _config_ref and _config_ref.target_market_mode == "crypto_short_window":
        return Path("dashboard/runtime_config_rd.json")
    return Path("dashboard/runtime_config.json")

RUNTIME_CONFIG_PATH_DEFAULT = Path("dashboard/runtime_config.json")

app = FastAPI(title="ReverseBot Dashboard", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth
api_key_header = APIKeyHeader(name="X-Dashboard-Key", auto_error=False)

async def verify_key(api_key: str = Depends(api_key_header)):
    if api_key != _get_password():
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid dashboard key")
    return api_key


# ─────────────────────────────────────────────
# DB Helpers
# ─────────────────────────────────────────────
async def _db():
    db_path = _get_db_path()
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    return db

async def get_stats(is_rd: bool = False) -> Dict[str, Any]:
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED'")
        total_closed = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl > 0")
        wins = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl <= 0")
        losses = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE order_status = 'FILLED'")
        total_pnl = float((await cursor.fetchone())[0])

        cursor = await db.execute(f"SELECT COALESCE(SUM(size_usdc), 0.0), COUNT(*) FROM {table} WHERE order_status IN ('OPEN', 'PENDING')")
        row = await cursor.fetchone()
        open_exposure = float(row[0])
        open_count = row[1]

        cursor = await db.execute("SELECT COUNT(*) FROM markets WHERE is_flagged = 1")
        flagged_markets = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM markets")
        total_markets = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT MAX(scanned_at) FROM scan_log")
        last_scan_row = await cursor.fetchone()
        last_scan = last_scan_row[0] if last_scan_row else None
        
        cursor = await db.execute(f"SELECT AVG(closed_at - opened_at) FROM {table} WHERE order_status = 'FILLED' AND closed_at IS NOT NULL AND opened_at IS NOT NULL")
        avg_hold_row = await cursor.fetchone()
        avg_hold = float(avg_hold_row[0]) if avg_hold_row and avg_hold_row[0] is not None else 0.0

        win_rate = round((wins / total_closed * 100) if total_closed > 0 else 0, 1)
        
        poll_interval = 1
        if _config_ref:
            poll_interval = getattr(_config_ref, 'poll_interval_seconds', 1)

        # 24h rolling realized net loss calculation
        start_of_window = int(time.time()) - 86400
        cursor = await db.execute(
            f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE closed_at >= ?",
            (start_of_window,)
        )
        net_pnl = float((await cursor.fetchone())[0])
        daily_loss = abs(net_pnl) if net_pnl < 0 else 0.0

        max_daily_loss = getattr(_config_ref, 'max_daily_loss_usdc', 75.0) if _config_ref else 75.0
        max_exposure = getattr(_config_ref, 'max_total_exposure_usdc', 50.0) if _config_ref else 50.0
        base_position_size = getattr(_config_ref, 'position_size_usdc', 10.0) if _config_ref else 10.0

        return {
            "total_closed": total_closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 4),
            "open_exposure": round(open_exposure, 2),
            "open_count": open_count,
            "flagged_markets": flagged_markets,
            "total_markets_scanned": total_markets,
            "last_scan_at": last_scan,
            "last_scan_duration_ms": getattr(_engine_ref, 'last_scan_duration_ms', 0) if _engine_ref else 0,
            "avg_hold_seconds": avg_hold,
            "poll_interval_seconds": poll_interval,
            "is_paper": _is_paper(),
            "engine_running": _engine_ref is not None and _engine_ref._task is not None and not _engine_ref._task.done(),
            "kill_switch_active": _kill_switch_ref.is_triggered if _kill_switch_ref else False,
            "daily_loss": round(daily_loss, 2),
            "max_daily_loss": max_daily_loss,
            "max_exposure": max_exposure,
            "base_position_size": base_position_size,
        }
    finally:
        await db.close()

async def get_positions(is_rd: bool = False) -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        sql = f"SELECT p.*, m.question, m.closes_at, m.yes_price, m.no_price, m.total_volume, m.yes_token_id, m.no_token_id FROM {table} p LEFT JOIN markets m ON p.market_id = m.market_id WHERE p.order_status IN ('OPEN', 'PENDING') ORDER BY p.opened_at DESC"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        result = []
        now = int(time.time())
        for row in rows:
            d = dict(row)
            
            # ─── SAFETY: Skip orphaned positions with missing market data ───
            if not d.get("question") or not d.get("market_id"):
                continue

            closes_at = d.get("closes_at") or 0
            time_left = max(0, closes_at - now)
            
            # --- LIVE PRICE WIRING ---
            side = d.get("side", "NO")
            yes_token_id = d.get("yes_token_id")
            no_token_id = d.get("no_token_id")
            
            db_yes = d.get("yes_price") or 0.5
            db_no = d.get("no_price") or 0.5
            
            live_yes = db_yes
            live_no = db_no
            
            # Try to fetch real-time price from WebSocket cache
            if _engine_ref and _engine_ref.ws_client:
                # 1. Resolve YES price
                ws_yes = None
                if yes_token_id:
                    ws_yes = _engine_ref.ws_client.bid_cache.get(yes_token_id)
                    # Fallback to implied price from NO ask
                    if (ws_yes is None or ws_yes <= 0) and no_token_id:
                        no_ask = _engine_ref.ws_client.ask_cache.get(no_token_id)
                        if no_ask is not None and no_ask < 1.0:
                            ws_yes = round(1.0 - no_ask, 4)
                    # Fallback to last trade price
                    if ws_yes is None or ws_yes <= 0:
                        ws_yes = _engine_ref.ws_client.price_cache.get(yes_token_id)
                
                # 2. Resolve NO price
                ws_no = None
                if no_token_id:
                    ws_no = _engine_ref.ws_client.bid_cache.get(no_token_id)
                    # Fallback to implied price from YES ask
                    if (ws_no is None or ws_no <= 0) and yes_token_id:
                        yes_ask = _engine_ref.ws_client.ask_cache.get(yes_token_id)
                        if yes_ask is not None and yes_ask < 1.0:
                            ws_no = round(1.0 - yes_ask, 4)
                    # Fallback to last trade price
                    if ws_no is None or ws_no <= 0:
                        ws_no = _engine_ref.ws_client.price_cache.get(no_token_id)
                
                # Update prices
                if ws_yes is not None and ws_yes > 0:
                    live_yes = ws_yes
                elif ws_no is not None and ws_no > 0:
                    live_yes = round(1.0 - ws_no, 4)
                    
                if ws_no is not None and ws_no > 0:
                    live_no = ws_no
                elif ws_yes is not None and ws_yes > 0:
                    live_no = round(1.0 - ws_yes, 4)
            
            # Now set the price for the side we actually bought
            live_bought_price = live_yes if side == "YES" else live_no
            live_pnl = 0.0
            
            # Calculate P&L using shares and net fee simulation (0.98)
            shares = d.get("shares_bought", 0.0)
            size = d.get("size_usdc", 1.0)
            partial_proceeds = 0.0
            
            raw_notes = d.get("notes")
            if raw_notes and raw_notes.startswith("{"):
                import json
                try:
                    notes_data = json.loads(raw_notes)
                    if "r" in notes_data:
                        shares = float(notes_data["r"])
                    if "p" in notes_data:
                        partial_proceeds = float(notes_data["p"])
                except Exception:
                    pass
            
            if shares >= 0 and live_bought_price is not None:
                current_value = live_bought_price * shares
                live_pnl = current_value + partial_proceeds - size
            
            d["live_yes"] = live_yes
            d["live_no"] = live_no
            d["live_bought_price"] = live_bought_price
            d["live_pnl"] = round(live_pnl, 4)
            d["shares_current"] = round(shares, 2)
            d["banked_usdc"] = round(partial_proceeds, 2)
            # --- END LIVE PRICE WIRING ---

            # Formatting as requested: ONLY minutes and seconds
            mins = time_left // 60
            secs = time_left % 60
            d["time_left_str"] = f"{mins}m {secs}s"
            if time_left == 0:
                d["time_left_str"] = "Resolving"
            d["time_left_sec"] = time_left
            result.append(d)
        return result
    finally:
        await db.close()

async def get_history(limit: int = 50, is_rd: bool = False) -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        sql = f"""
            SELECT p.*, m.question, s.streak_depth
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            LEFT JOIN (
                SELECT market_id, MAX(streak_depth) as streak_depth
                FROM signals
                GROUP BY market_id
            ) s ON p.market_id = s.market_id
            WHERE p.order_status = 'FILLED'
            ORDER BY p.closed_at DESC
            LIMIT ?
        """
        cursor = await db.execute(sql, (limit,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()

async def get_filtered_history(
    is_rd: bool = False,
    day: Optional[str] = None,
    hour_et: Optional[int] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    window_minutes: Optional[int] = None,
    streak_depth: Optional[int] = None,
    limit: int = 1000
) -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        sql = f"""
            SELECT p.*, m.question, s.streak_depth
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            LEFT JOIN (
                SELECT market_id, MAX(streak_depth) as streak_depth
                FROM signals
                GROUP BY market_id
            ) s ON p.market_id = s.market_id
            WHERE p.order_status = 'FILLED'
            ORDER BY p.closed_at DESC
            LIMIT 5000
        """
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone, timedelta
        
        tz_et = ZoneInfo("America/New_York")
        tz_ist = ZoneInfo("Asia/Kolkata")
        
        results = []
        now_et = datetime.now(timezone.utc).astimezone(tz_et)
        today_str = now_et.strftime("%Y-%m-%d")
        yesterday_str = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        
        for r in rows:
            d = dict(r)
            opened_at = d.get("opened_at")
            closed_at = d.get("closed_at")
            
            if not opened_at:
                continue
                
            dt_utc_open = datetime.fromtimestamp(opened_at, tz=timezone.utc)
            dt_et_open = dt_utc_open.astimezone(tz_et)
            dt_ist_open = dt_utc_open.astimezone(tz_ist)
            
            d["opened_at_et"] = dt_et_open.strftime("%Y-%m-%d %H:%M:%S ET")
            d["opened_at_ist"] = dt_ist_open.strftime("%Y-%m-%d %H:%M:%S IST")
            
            trade_day_str = dt_et_open.strftime("%Y-%m-%d")
            trade_hour_et = dt_et_open.hour
            
            if closed_at:
                dt_utc_close = datetime.fromtimestamp(closed_at, tz=timezone.utc)
                d["closed_at_et"] = dt_utc_close.astimezone(tz_et).strftime("%Y-%m-%d %H:%M:%S ET")
                d["closed_at_ist"] = dt_utc_close.astimezone(tz_ist).strftime("%Y-%m-%d %H:%M:%S IST")
            else:
                d["closed_at_et"] = None
                d["closed_at_ist"] = None
                
            if day:
                target_day = day.strip().lower()
                if target_day == "today":
                    if trade_day_str != today_str: continue
                elif target_day == "yesterday":
                    if trade_day_str != yesterday_str: continue
                elif target_day != "all":
                    if trade_day_str != day: continue
                    
            if hour_et is not None:
                if trade_hour_et != hour_et: continue
                
            if symbol:
                if d.get("symbol", "").upper() != symbol.upper(): continue
                
            if side:
                if d.get("side", "").upper() != side.upper(): continue

            if window_minutes is not None:
                if d.get("window_minutes") != window_minutes: continue

            if streak_depth is not None:
                if d.get("streak_depth") != streak_depth: continue
                
            results.append(d)
            if len(results) >= limit:
                break
                
        return results
    finally:
        await db.close()

async def get_pnl_series(is_rd: bool = False) -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        sql = f"SELECT closed_at, realised_pnl FROM {table} WHERE order_status = 'FILLED' ORDER BY closed_at ASC"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        running = 0.0
        series = []
        for row in rows:
            running += row["realised_pnl"] or 0
            series.append({"ts": row["closed_at"], "pnl": round(running, 4)})
        return series
    finally:
        await db.close()

# ─────────────────────────────────────────────
# Runtime Config
# ─────────────────────────────────────────────
DEFAULT_RUNTIME_CONFIG = {
    "position_size_usdc": 2.0,
    "max_open_positions": 15,
    "max_daily_loss_usdc": 75.0,
    "max_total_exposure_usdc": 50.0,
    "min_ev_threshold": 0.02,
    "global_cooldown": 60,
    "hold_to_resolution": False,
    "max_no_ask_multiplier": 1.35,
    "high_vol_ask_multiplier": 1.6,
    "high_vol_assets": "HYPE,XRP,DOGE",
    "max_spread_pct_of_ask": 0.60,
    "ghost_guard_enabled": False,
    "short_window_min_seconds_to_close": 60,
    "min_seconds_to_close_pre_entry": 180,
    "price_velocity_max": 0.025,
    "price_velocity_window": 5,
    "_TESTING_only_15min_windows": False,
    "early_phase_min_seconds": 43200,
    "early_yes_bias_max": 0.85,
    "mid_phase_min_seconds": 14400,
    "mid_yes_bias_max": 0.80,
    "late_yes_bias_max": 0.75,
    "min_entry_price": 0.45,
    "max_entry_price": 0.65,
}

def _get_runtime_config_path_internal() -> Path:
    return _get_runtime_config_path()

def load_runtime_config() -> Dict:
    path = _get_runtime_config_path_internal()
    cfg = DEFAULT_RUNTIME_CONFIG.copy()
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            cfg.update(loaded)
        except Exception: pass
    return cfg

def save_runtime_config(cfg: Dict) -> None:
    path = _get_runtime_config_path_internal()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))

class ConfigUpdate(BaseModel):
    key: str
    value: float

# ─────────────────────────────────────────────
# Connection Manager
# ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
    async def broadcast_json(self, data: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(data)
            except Exception:
                pass

manager = ConnectionManager()

async def push_debug_event(level: str, msg: str, detail: str = None, meta: dict = None):
    await manager.broadcast_json({
        "type": "debug",
        "level": level.upper(),
        "msg": msg,
        "detail": detail,
        "meta": meta
    })

async def push_scan_feed(markets: list):
    await manager.broadcast_json({
        "type": "scan_feed",
        "markets": markets
    })

# ─────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────
@app.get("/api/stats")
@app.get("/rd/api/stats")
async def api_stats(request: Request, type: str = None, _: str = Depends(verify_key)):
    is_rd = _is_paper() or (type == "rd") or ("/rd/" in str(request.url))
    return await get_stats(is_rd=is_rd)

@app.get("/api/positions")
@app.get("/rd/api/positions")
async def api_positions(request: Request, type: str = None, _: str = Depends(verify_key)):
    is_rd = _is_paper() or (type == "rd") or ("/rd/" in str(request.url))
    return await get_positions(is_rd=is_rd)

@app.get("/api/history")
@app.get("/rd/api/history")
async def api_history(request: Request, limit: int = 50, type: str = None, _: str = Depends(verify_key)):
    is_rd = _is_paper() or (type == "rd") or ("/rd/" in str(request.url))
    return await get_history(limit, is_rd=is_rd)

@app.get("/api/history-filtered")
@app.get("/rd/api/history-filtered")
async def api_history_filtered(
    request: Request,
    day: Optional[str] = None,
    hour_et: Optional[int] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    window_minutes: Optional[int] = None,
    streak_depth: Optional[int] = None,
    limit: int = 1000,
    type: str = None,
    _: str = Depends(verify_key)
):
    is_rd = _is_paper() or (type == "rd") or ("/rd/" in str(request.url))
    return await get_filtered_history(
        is_rd=is_rd,
        day=day,
        hour_et=hour_et,
        symbol=symbol,
        side=side,
        window_minutes=window_minutes,
        streak_depth=streak_depth,
        limit=limit
    )

@app.get("/api/pnl-series")
@app.get("/rd/api/pnl-series")
async def api_pnl_series(request: Request, type: str = None, _: str = Depends(verify_key)):
    is_rd = _is_paper() or (type == "rd") or ("/rd/" in str(request.url))
    return await get_pnl_series(is_rd=is_rd)

@app.get("/api/config")
@app.get("/rd/api/config")
async def api_get_config(_: str = Depends(verify_key)):
    return load_runtime_config()

@app.post("/api/config")
@app.post("/rd/api/config")
async def api_set_config(update: ConfigUpdate, _: str = Depends(verify_key)):
    cfg = load_runtime_config()
    if update.key not in cfg: raise HTTPException(status_code=400, detail="Invalid config key")
    
    # Handle booleans safely from float/int inputs (1.0 -> True, 0.0 -> False)
    if update.key in ["hold_to_resolution", "trailing_tp_enabled", "ghost_guard_enabled", "_TESTING_only_15min_windows"]:
        cfg[update.key] = bool(update.value >= 0.5)
    elif isinstance(cfg[update.key], int):
        cfg[update.key] = int(update.value)
    else:
        cfg[update.key] = update.value
        
    save_runtime_config(cfg)
    if _engine_ref:
        from config.config import load_runtime_overrides
        _engine_ref.config = load_runtime_overrides(_engine_ref.config)
    return {"status": "ok", "message": f"Updated {update.key}"}

@app.post("/api/control/{action}")
@app.post("/rd/api/control/{action}")
async def api_control(action: str, _: str = Depends(verify_key)):
    if not _engine_ref: return {"ok": False, "message": "Engine not initialized"}
    if action == "start":
        await _engine_ref.start()
        return {"ok": True, "message": "Engine started"}
    elif action == "stop":
        await _engine_ref.stop()
        return {"ok": True, "message": "Engine stopped"}
    elif action == "kill":
        if _kill_switch_ref: _kill_switch_ref.trigger("Manual Dashboard Kill")
        return {"ok": True, "message": "KILL SWITCH TRIGGERED"}
    raise HTTPException(status_code=400, detail="Invalid action")

@app.post("/api/positions/{position_id}/close")
@app.post("/rd/api/positions/{position_id}/close")
async def api_close_position(position_id: str, _: str = Depends(verify_key)):
    if not _engine_ref or not getattr(_engine_ref, 'position_mgr', None):
        raise HTTPException(status_code=400, detail="Engine not initialized")
    await _engine_ref.position_mgr.manual_close_position(position_id)
    return {"status": "ok", "message": f"Closure triggered for {position_id}"}

@app.post("/api/history/clear")
@app.post("/rd/api/history/clear")
async def api_clear_history(request: Request, type: str = None, _: str = Depends(verify_key)):
    is_rd = (type == "rd") or ("/rd/" in str(request.url))
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        await db.execute(f"DELETE FROM {table} WHERE order_status IN ('CLOSED', 'REJECTED', 'CANCELLED', 'FILLED')")
        await db.commit()
        return {"status": "ok", "message": "History cleared"}
    finally:
        await db.close()

@app.delete("/api/history/{position_id}")
@app.delete("/rd/api/history/{position_id}")
async def api_delete_history(request: Request, position_id: str, type: str = None, _: str = Depends(verify_key)):
    is_rd = (type == "rd") or ("/rd/" in str(request.url))
    db = await _db()
    try:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        await db.execute(f"DELETE FROM {table} WHERE position_id = ?", (position_id,))
        await db.commit()
        return {"status": "ok", "message": f"Deleted {position_id}"}
    finally:
        await db.close()

@app.websocket("/ws")
@app.websocket("/rd/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("key", "")
    if token != _get_password():
        await websocket.close(code=4003)
        return
    
    is_rd = _is_paper() or "/rd/" in str(websocket.url) or websocket.query_params.get("type") == "rd"
    await manager.connect(websocket)
    try:
        while True:
            try:
                # Get latest stats and positions
                stats = await get_stats(is_rd=is_rd)
                positions = await get_positions(is_rd=is_rd)
                now = int(time.time())
                
                # Enrich positions with live price from engine cache
                if _engine_ref and _engine_ref.position_mgr and _engine_ref.position_mgr.ws:
                    price_cache = _engine_ref.position_mgr.ws.price_cache
                    bid_cache = _engine_ref.position_mgr.ws.bid_cache
                    for p in positions:
                        side = p.get("side", "NO")
                        yes_token_id = p.get("yes_token_id")
                        no_token_id = p.get("no_token_id")
                        
                        db_yes = p.get("yes_price") or 0.5
                        db_no = p.get("no_price") or 0.5
                        
                        live_yes = db_yes
                        live_no = db_no
                        
                        # 1. Resolve YES price
                        ws_yes = None
                        if yes_token_id:
                            ws_yes = bid_cache.get(yes_token_id)
                            # Fallback to implied price from NO ask
                            if (ws_yes is None or ws_yes <= 0) and no_token_id:
                                no_ask = _engine_ref.position_mgr.ws.ask_cache.get(no_token_id)
                                if no_ask is not None and no_ask < 1.0:
                                    ws_yes = round(1.0 - no_ask, 4)
                            # Fallback to last trade price
                            if ws_yes is None or ws_yes <= 0:
                                ws_yes = price_cache.get(yes_token_id)
                        
                        # 2. Resolve NO price
                        ws_no = None
                        if no_token_id:
                            ws_no = bid_cache.get(no_token_id)
                            # Fallback to implied price from YES ask
                            if (ws_no is None or ws_no <= 0) and yes_token_id:
                                yes_ask = _engine_ref.position_mgr.ws.ask_cache.get(yes_token_id)
                                if yes_ask is not None and yes_ask < 1.0:
                                    ws_no = round(1.0 - yes_ask, 4)
                            # Fallback to last trade price
                            if ws_no is None or ws_no <= 0:
                                ws_no = price_cache.get(no_token_id)
                        
                        # Update prices
                        if ws_yes is not None and ws_yes > 0:
                            live_yes = ws_yes
                        elif ws_no is not None and ws_no > 0:
                            live_yes = round(1.0 - ws_no, 4)
                            
                        if ws_no is not None and ws_no > 0:
                            live_no = ws_no
                        elif ws_yes is not None and ws_yes > 0:
                            live_no = round(1.0 - ws_yes, 4)
                        
                        live_bought_price = live_yes if side == "YES" else live_no
                        p["live_yes"] = live_yes
                        p["live_no"] = live_no
                        p["live_bought_price"] = live_bought_price
                        
                        # Handle persistence: 'notes' might be JSON if it's a stabilized trade
                        tracking = {}
                        if p.get("notes") and p["notes"].startswith("{"):
                            try:
                                tracking = json.loads(p["notes"])
                            except: pass
                            
                        shares_rem = float(tracking.get("r", p.get("shares_bought", 0.0)))
                        partial_proceeds = float(tracking.get("p", 0.0))
                        
                        if live_bought_price is not None:
                            current_value = (live_bought_price * shares_rem) + partial_proceeds
                            cost_basis = float(p.get("size_usdc") or 0.0)
                            p["live_pnl"] = round(current_value - cost_basis, 4)
                            p["shares_remaining"] = shares_rem
                            p["shares_current"] = round(shares_rem, 2)
                            p["banked_usdc"] = round(partial_proceeds, 2)
                        else:
                            # Fallback for resolved markets
                            if p.get("order_status") in ("FILLED", "MARKET_OUTCOME"):
                                p["live_pnl"] = p.get("realised_pnl", 0.0)
                            else:
                                p["live_pnl"] = 0.0

                # Fetch recent resolved candles
                candles = {}
                streaks = {}
                current_et = ""
                if _engine_ref and _engine_ref.scanner:
                    import datetime
                    from zoneinfo import ZoneInfo
                    
                    # Current ET
                    dt_utc = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
                    dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                    current_et = dt_et.strftime("%Y-%m-%d %H:%M:%S ET")
                    
                    def _format_ts_range_et(ts_start: int, duration_min: int) -> str:
                        d_start_utc = datetime.datetime.fromtimestamp(ts_start, tz=datetime.timezone.utc)
                        d_start_et = d_start_utc.astimezone(ZoneInfo("America/New_York"))
                        d_end_utc = datetime.datetime.fromtimestamp(ts_start + duration_min * 60, tz=datetime.timezone.utc)
                        d_end_et = d_end_utc.astimezone(ZoneInfo("America/New_York"))
                        return f"{d_start_et.strftime('%m/%d %H:%M')}-{d_end_et.strftime('%H:%M')} ET"
                        
                    for symbol in ("BTC", "ETH"):
                        for dur in (5, 15):
                            closes_at = ((now // (dur * 60)) + 1) * (dur * 60)
                            dur_secs = dur * 60
                            t1 = closes_at - (2 * dur_secs)
                            t2 = closes_at - (3 * dur_secs)
                            
                            coin = symbol.lower()
                            slug1 = f"{coin}-updown-{dur}m-{t1}"
                            slug2 = f"{coin}-updown-{dur}m-{t2}"
                            
                            try:
                                outcomes = await _engine_ref.scanner._get_historical_outcomes(symbol, dur, closes_at)
                                candles[f"{symbol}_{dur}m"] = {
                                    "t1": {
                                        "slug": slug1,
                                        "time_et": _format_ts_range_et(t1, dur),
                                        "outcome": outcomes.get("out1", "?"),
                                        "source": outcomes.get("src1", "UNKNOWN"),
                                        "delta_bps": outcomes.get("delta1", 0.0)
                                    },
                                    "t2": {
                                        "slug": slug2,
                                        "time_et": _format_ts_range_et(t2, dur),
                                        "outcome": outcomes.get("out2", "?"),
                                        "source": outcomes.get("src2", "UNKNOWN"),
                                        "delta_bps": outcomes.get("delta2", 0.0)
                                    }
                                }
                                
                                out1 = outcomes.get("out1", "?")
                                out2 = outcomes.get("out2", "?")
                                out3 = outcomes.get("out3", "?")
                                out4 = outcomes.get("out4", "?")
                                out5 = outcomes.get("out5", "?")
                                out6 = outcomes.get("out6", "?")
                                
                                depth = 0
                                direction = "?"
                                if out1 != "?" and out2 != "?":
                                    if out1 == out2:
                                        depth = 2
                                        direction = out1
                                        if out3 != "?" and out3 == out1:
                                            depth = 3
                                            if out4 != "?" and out4 == out1:
                                                depth = 4
                                                if out5 != "?" and out5 == out1:
                                                    depth = 5
                                                    if out6 != "?" and out6 == out1:
                                                        depth = 6
                                    else:
                                        depth = 1
                                        direction = out1
                                streaks[f"{symbol}_{dur}m"] = {"depth": depth, "direction": direction}
                            except Exception as o_err:
                                logger.warning(f"Failed resolving outcomes for {symbol} {dur}m: {o_err}")

                # Send update to frontend only if connected
                if websocket.client_state.value == 1: # CONNECTED
                    await websocket.send_json({
                        "type": "update",
                        "stats": stats,
                        "positions": positions,
                        "candles": candles,
                        "streaks": streaks,
                        "current_et": current_et,
                        "ts": now
                    })
                else:
                    break
            except (WebSocketDisconnect, starlette.websockets.WebSocketDisconnect, ConnectionResetError):
                break
            except Exception as e:
                import traceback
                err_name = type(e).__name__
                if (
                    "Disconnect" in err_name or
                    "ConnectionClosed" in err_name or
                    "ConnectionReset" in err_name or
                    "ClientDisconnected" in err_name
                ):
                    break
                logger.error(f"Iteration error: {e}\n{traceback.format_exc()}")
                if websocket.client_state.value == 1:
                    try:
                        await websocket.send_json({"type": "error", "message": str(e)})
                    except:
                        break
                else:
                    break
            
            await asyncio.sleep(1) # Fast 1s updates for the new dashboard
    except (WebSocketDisconnect, starlette.websockets.WebSocketDisconnect):
        # Normal browser tab closure — no action needed
        pass
    except Exception as e:
        import traceback
        err_name = type(e).__name__
        if (
            "Disconnect" in err_name or
            "ConnectionClosed" in err_name or
            "ConnectionReset" in err_name or
            "ClientDisconnected" in err_name
        ):
            pass
        else:
            err_msg = f"Dashboard WS error: {e}\n{traceback.format_exc()}"
            logger.error(err_msg)
    finally:
        manager.disconnect(websocket)

# ─────────────────────────────────────────────
# Static Serving
# ─────────────────────────────────────────────
dashboard_dir = Path(__file__).parent.parent.parent / "dashboard"

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    from config.config import load_config
    try:
        # For the HTML serving, we check the specific process config
        if _config_ref:
            page = dashboard_dir / ("rd.html" if _config_ref.target_market_mode == "crypto_short_window" else "index.html")
        else:
            cfg = load_config()
            page = dashboard_dir / ("rd.html" if cfg.target_market_mode == "crypto_short_window" else "index.html")
    except: page = dashboard_dir / "index.html"
    if page.exists(): return HTMLResponse(page.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)

@app.get("/rd", response_class=HTMLResponse)
async def serve_rd_dashboard():
    rd = dashboard_dir / "rd.html"
    if rd.exists(): return HTMLResponse(rd.read_text())
    return HTMLResponse("<h1>R&D Dashboard not found</h1>", status_code=404)

async def start_dashboard_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
