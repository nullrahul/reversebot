"""
Web Dashboard API — Polymarket Reverse Bot.
FastAPI server exposing real-time stats, positions, history, and runtime parameter control.

Endpoints:
  GET  /api/stats          - Performance summary
  GET  /api/positions      - Live open positions
  GET  /api/history        - Closed trade history  
  GET  /api/config         - Current runtime config
  POST /api/config         - Update runtime config (hot-reload)
  POST /api/control/start  - Start trading engine
  POST /api/control/stop   - Stop trading engine
  POST /api/control/kill   - Emergency kill switch
  WS   /ws                 - Live push updates every 5s
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.status import HTTP_403_FORBIDDEN

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "reversebot2025")
DB_PATH = os.getenv("DB_PATH", "data/reversebot.db")
RUNTIME_CONFIG_PATH = Path("dashboard/runtime_config.json")
PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"

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
    if api_key != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid dashboard key")
    return api_key

# Engine reference (injected by main.py)
_engine_ref = None
_kill_switch_ref = None

def set_engine(engine, kill_switch):
    global _engine_ref, _kill_switch_ref
    _engine_ref = engine
    _kill_switch_ref = kill_switch


# ─────────────────────────────────────────────
# DB Helpers
# ─────────────────────────────────────────────
async def _db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def get_stats() -> Dict[str, Any]:
    db = await _db()
    try:
        paper = PAPER_TRADE
        table = "paper_positions" if paper else "positions"
        
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED'")
        total_closed = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl > 0")
        wins = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl < 0")
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

        # Last scan time
        cursor = await db.execute("SELECT MAX(scanned_at) FROM scan_log")
        last_scan_row = await cursor.fetchone()
        last_scan = last_scan_row[0] if last_scan_row else None

        win_rate = round((wins / total_closed * 100) if total_closed > 0 else 0, 1)

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
            "is_paper": paper,
            "engine_running": _engine_ref is not None and _engine_ref._task is not None and not _engine_ref._task.done(),
            "kill_switch_active": _kill_switch_ref.is_triggered if _kill_switch_ref else False,
        }
    finally:
        await db.close()


async def get_positions() -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if PAPER_TRADE else "positions"
        cursor = await db.execute(
            f"""
            SELECT p.*, m.question, m.closes_at, m.yes_price, m.no_price, m.total_volume
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            WHERE p.order_status IN ('OPEN', 'PENDING')
            ORDER BY p.opened_at DESC
            """
        )
        rows = await cursor.fetchall()
        result = []
        now = int(time.time())
        for row in rows:
            d = dict(row)
            closes_at = d.get("closes_at") or 0
            time_left = max(0, closes_at - now)
            days = time_left // 86400
            hours = (time_left % 86400) // 3600
            d["time_left_str"] = f"{days}d {hours}h" if time_left > 0 else "Resolving"
            d["time_left_sec"] = time_left
            result.append(d)
        return result
    finally:
        await db.close()


async def get_history(limit: int = 50) -> List[Dict]:
    db = await _db()
    try:
        table = "paper_positions" if PAPER_TRADE else "positions"
        cursor = await db.execute(
            f"""
            SELECT p.*, m.question
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            WHERE p.order_status = 'FILLED'
            ORDER BY p.closed_at DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_pnl_series() -> List[Dict]:
    """Running P&L over time for the chart."""
    db = await _db()
    try:
        table = "paper_positions" if PAPER_TRADE else "positions"
        cursor = await db.execute(
            f"SELECT closed_at, realised_pnl FROM {table} WHERE order_status = 'FILLED' ORDER BY closed_at ASC"
        )
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
    "yes_bias_threshold": 0.80,
    "yes_bias_max_threshold": 0.97,
    "min_volume_24h_usdc": 500.0,
    "min_time_to_close_hours": 6.0,
    "max_time_to_close_days": 7.0,
    "position_size_usdc": 2.0,
    "max_open_positions": 2,
    "max_daily_loss_usdc": 5.0,
    "max_total_exposure_usdc": 5.0,
    "take_profit_multiplier": 3.0,
    "stop_loss_multiplier": 0.5,
    "hold_to_resolution": False,
    "poll_interval_seconds": 60,
}

def load_runtime_config() -> Dict:
    if RUNTIME_CONFIG_PATH.exists():
        try:
            return json.loads(RUNTIME_CONFIG_PATH.read_text())
        except Exception:
            pass
    return DEFAULT_RUNTIME_CONFIG.copy()

def save_runtime_config(cfg: Dict) -> None:
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ─────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats(_: str = Depends(verify_key)):
    return await get_stats()


@app.get("/api/positions")
async def api_positions(_: str = Depends(verify_key)):
    return await get_positions()


@app.get("/api/history")
async def api_history(limit: int = 50, _: str = Depends(verify_key)):
    return await get_history(limit)


@app.get("/api/pnl-series")
async def api_pnl_series(_: str = Depends(verify_key)):
    return await get_pnl_series()


@app.get("/api/config")
async def api_get_config(_: str = Depends(verify_key)):
    return load_runtime_config()


class ConfigUpdate(BaseModel):
    key: str
    value: Any


@app.post("/api/config")
async def api_update_config(update: ConfigUpdate, _: str = Depends(verify_key)):
    cfg = load_runtime_config()
    if update.key not in DEFAULT_RUNTIME_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {update.key}")
    
    # Type coerce
    expected_type = type(DEFAULT_RUNTIME_CONFIG[update.key])
    try:
        cfg[update.key] = expected_type(update.value)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid value for {update.key}: {e}")
    
    save_runtime_config(cfg)
    return {"ok": True, "key": update.key, "new_value": cfg[update.key], "message": f"✅ {update.key} updated to {cfg[update.key]}"}


@app.post("/api/control/start")
async def api_start(_: str = Depends(verify_key)):
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    result = await _engine_ref.start()
    return {"ok": result, "message": "✅ Engine started" if result else "⚠️ Engine already running"}


@app.post("/api/control/stop")
async def api_stop(_: str = Depends(verify_key)):
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    result = await _engine_ref.stop()
    return {"ok": result, "message": "🛑 Engine stopped" if result else "⚠️ Engine not running"}


@app.post("/api/control/kill")
async def api_kill(_: str = Depends(verify_key)):
    if _kill_switch_ref is None:
        raise HTTPException(status_code=503, detail="Kill switch not initialized")
    asyncio.create_task(_kill_switch_ref.trigger("DASHBOARD_KILL", {"triggered_by": "web_dashboard"}))
    return {"ok": True, "message": "🚨 Kill switch triggered — cancelling all orders"}


@app.post("/api/positions/{position_id}/close")
async def api_close_position(position_id: str, _: str = Depends(verify_key)):
    if _engine_ref is None or _engine_ref.position_mgr is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    try:
        await _engine_ref.position_mgr.manual_close_position(position_id)
        return {"ok": True, "message": f"Closing position {position_id}..."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class TPSLUpdate(BaseModel):
    tp_multiplier: float
    sl_multiplier: float


@app.post("/api/positions/{position_id}/tpsl")
async def api_update_tpsl(position_id: str, update: TPSLUpdate, _: str = Depends(verify_key)):
    overrides_path = Path("dashboard/position_overrides.json")
    overrides = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text())
        except Exception: 
            pass
    
    overrides[position_id] = {
        "tp_multiplier": update.tp_multiplier,
        "sl_multiplier": update.sl_multiplier
    }
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides_path.write_text(json.dumps(overrides, indent=2))
    return {"ok": True, "message": f"Updated TP/SL for {position_id}"}


@app.post("/api/history/clear")
async def api_clear_history(_: str = Depends(verify_key)):
    db = await _db()
    try:
        table = "paper_positions" if PAPER_TRADE else "positions"
        await db.execute(f"DELETE FROM {table} WHERE order_status = 'FILLED'")
        await db.commit()
        return {"ok": True, "message": "History cleared"}
    finally:
        await db.close()


@app.delete("/api/history/{position_id}")
async def api_delete_history(position_id: str, _: str = Depends(verify_key)):
    db = await _db()
    try:
        table = "paper_positions" if PAPER_TRADE else "positions"
        await db.execute(f"DELETE FROM {table} WHERE position_id = ? AND order_status = 'FILLED'", (position_id,))
        await db.commit()
        return {"ok": True, "message": f"Deleted {position_id}"}
    finally:
        await db.close()


# ─────────────────────────────────────────────
# WebSocket — Live Push
# ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws) if hasattr(self.active, 'discard') else None
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: Dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Simple token check via query param
    token = websocket.query_params.get("key", "")
    if token != DASHBOARD_PASSWORD:
        await websocket.close(code=4003)
        return

    await manager.connect(websocket)
    try:
        while True:
            stats = await get_stats()
            positions = await get_positions()

            # Inject Live Prices & Real-time Countdown
            now = int(time.time())
            if _engine_ref and _engine_ref.position_mgr and getattr(_engine_ref.position_mgr, 'ws', None):
                cache = _engine_ref.position_mgr.ws.price_cache
                for p in positions:
                    m_id = p["market_id"]
                    shares = float(p.get("shares_bought") or 0.0)
                    size = float(p.get("size_usdc") or 0.0)
                    closes_at = p.get("closes_at") or 0
                    
                    # Update countdown
                    time_left = max(0, closes_at - now)
                    days = time_left // 86400
                    hours = (time_left % 86400) // 3600
                    mins = (time_left % 3600) // 60
                    if days > 0:
                        p["time_left_str"] = f"{days}d {hours}h"
                    elif hours > 0:
                        p["time_left_str"] = f"{hours}h {mins}m"
                    else:
                        p["time_left_str"] = f"{mins}m" if mins > 0 else "Resolving"
                    
                    if m_id in cache:
                        live_no = 1.0 - cache[m_id]
                        p["live_yes"] = round(cache[m_id], 4)
                        p["live_no"] = round(live_no, 4)
                        # Fee-adjusted P&L (2% on winnings)
                        p["live_pnl"] = round((live_no * shares * 0.98) - size, 4)
                    else:
                        p["live_yes"] = float(p.get("yes_price") or 0.0)
                        p["live_no"] = float(p.get("no_price") or 0.0)
                        p["live_pnl"] = 0.0
            else:
                for p in positions:
                    p["live_yes"] = float(p.get("yes_price") or 0.0)
                    p["live_no"] = float(p.get("no_price") or 0.0)
                    p["live_pnl"] = 0.0
                    p["time_left_str"] = "—"

            await websocket.send_json({
                "type": "update",
                "stats": stats,
                "positions": positions,
                "ts": int(time.time()),
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        import traceback
        traceback.print_exc()
        manager.disconnect(websocket)


# ─────────────────────────────────────────────
# Serve the SPA
# ─────────────────────────────────────────────
dashboard_dir = Path(__file__).parent.parent.parent / "dashboard"

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index = dashboard_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


async def start_dashboard_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
