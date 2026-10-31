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
    db = await aiosqlite.connect(db_path, timeout=60.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 60000")
    return db

async def _db_ro():
    db_path = _get_db_path()
    abs_path = os.path.abspath(db_path)
    db = await aiosqlite.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=10.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 10000")
    return db

async def get_stats(is_rd: bool = False, profile: Optional[str] = None) -> Dict[str, Any]:
    db = await _db_ro()
    try:
        if profile == "executioner":
            table = "executioner_positions"
            cond = ""
            params_count = ()
            params_pnl = ()
        elif profile == "runtime":
            table = "positions"
            cond = ""
            params_count = ()
            params_pnl = ()
        elif profile and profile != "main":
            table = "profile_positions"
            cond = "AND profile_name = ?"
            params_count = (profile,)
            params_pnl = (profile,)
        else:
            table = "paper_positions" if (is_rd or _is_paper()) else "positions"
            cond = ""
            params_count = ()
            params_pnl = ()

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' {cond}", params_count)
        total_closed = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl > 0 {cond}", params_count)
        wins = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE order_status = 'FILLED' AND realised_pnl <= 0 {cond}", params_count)
        losses = (await cursor.fetchone())[0]

        cursor = await db.execute(f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE order_status = 'FILLED' {cond}", params_pnl)
        total_pnl = float((await cursor.fetchone())[0])

        cursor = await db.execute(f"SELECT COALESCE(SUM(size_usdc), 0.0), COUNT(*) FROM {table} WHERE order_status IN ('OPEN', 'PENDING') {cond}", params_count)
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
        
        cursor = await db.execute(f"SELECT AVG(closed_at - opened_at) FROM {table} WHERE order_status = 'FILLED' AND closed_at IS NOT NULL AND opened_at IS NOT NULL {cond}", params_pnl)
        avg_hold_row = await cursor.fetchone()
        avg_hold = float(avg_hold_row[0]) if avg_hold_row and avg_hold_row[0] is not None else 0.0

        win_rate = round((wins / total_closed * 100) if total_closed > 0 else 0, 1)
        
        poll_interval = 1
        if _config_ref:
            poll_interval = getattr(_config_ref, 'poll_interval_seconds', 1)

        # 24h rolling realized net loss calculation
        start_of_window = int(time.time()) - 86400
        if cond:
            cursor = await db.execute(
                f"SELECT COALESCE(SUM(realised_pnl), 0.0) FROM {table} WHERE closed_at >= ? AND profile_name = ?",
                (start_of_window, profile)
            )
        else:
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
            "is_paper": False if profile == "runtime" else (is_rd or _is_paper() or (profile is not None and profile != "main")),
            "engine_running": _engine_ref is not None and _engine_ref._task is not None and not _engine_ref._task.done(),
            "kill_switch_active": _kill_switch_ref.is_triggered if _kill_switch_ref else False,
            "daily_loss": round(daily_loss, 2),
            "max_daily_loss": max_daily_loss,
            "max_exposure": max_exposure,
            "base_position_size": base_position_size,
        }
    finally:
        await db.close()

async def get_positions(is_rd: bool = False, profile: Optional[str] = None) -> List[Dict]:
    db = await _db_ro()
    try:
        if profile == "runtime":
            table = "positions"
            cond = "WHERE p.order_status IN ('OPEN', 'PENDING')"
            params = ()
        elif profile and profile != "main":
            table = "profile_positions"
            cond = "WHERE p.profile_name = ? AND p.order_status IN ('OPEN', 'PENDING')"
            params = (profile,)
        else:
            table = "paper_positions" if (is_rd or _is_paper()) else "positions"
            cond = "WHERE p.order_status IN ('OPEN', 'PENDING')"
            params = ()
        sql = f"SELECT p.*, m.question, m.closes_at, m.yes_price, m.no_price, m.total_volume, m.yes_token_id, m.no_token_id FROM {table} p LEFT JOIN markets m ON p.market_id = m.market_id {cond} ORDER BY p.opened_at DESC"
        cursor = await db.execute(sql, params)
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
                        if shares <= 0.0 and d.get("order_status") in ("OPEN", "PENDING"):
                            shares = float(d.get("shares_bought", 0.0))
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

async def get_history(limit: int = 50, is_rd: bool = False, profile: Optional[str] = None) -> List[Dict]:
    db = await _db_ro()
    try:
        if profile == "runtime":
            table = "positions"
            cond = "WHERE p.order_status = 'FILLED'"
            params = (limit,)
        elif profile and profile != "main":
            table = "profile_positions"
            cond = "WHERE p.profile_name = ? AND p.order_status = 'FILLED'"
            params = (profile, limit)
        else:
            table = "paper_positions" if (is_rd or _is_paper()) else "positions"
            cond = "WHERE p.order_status = 'FILLED'"
            params = (limit,)
        sql = f"""
            SELECT p.*, m.question
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            {cond}
            ORDER BY p.closed_at DESC
            LIMIT ?
        """
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("streak_depth") is None:
                notes_str = d.get("notes")
                if notes_str and "streak_depth" in notes_str:
                    try:
                        parsed_notes = json.loads(notes_str)
                        d["streak_depth"] = parsed_notes.get("streak_depth")
                    except Exception:
                        pass
            result.append(d)
        return result
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
    limit: int = 1000,
    page: int = 1,
    max_drawdown_op: Optional[str] = None,
    max_drawdown_val: Optional[float] = None,
    entry_price_op: Optional[str] = None,
    entry_price_val: Optional[float] = None,
    entry_price_min: Optional[float] = None,
    entry_price_max: Optional[float] = None,
    time_remaining_op: Optional[str] = None,
    time_remaining_val: Optional[float] = None,
    time_remaining_min: Optional[float] = None,
    time_remaining_max: Optional[float] = None,
    exit_price_op: Optional[str] = None,
    exit_price_val: Optional[float] = None,
    profile: Optional[str] = None,
    base_profile: Optional[str] = None,
    htr_mode: Optional[int] = 0
) -> Dict[str, Any]:
    db = await _db_ro()
    try:
        if profile == "executioner":
            table = "executioner_positions"
            params = ()
            if base_profile and base_profile != "all":
                bp = base_profile.lower().strip()
                if bp in ("req_1", "1", "stop_loss"):
                    cond = "WHERE p.base_profile_name = 'stop_loss' OR p.request_id = 1"
                elif bp in ("req_2", "2", "cross_market_hedge"):
                    cond = "WHERE p.base_profile_name = 'cross_market_hedge' OR p.request_id = 2"
                elif bp in ("req_3", "3", "reversal_probability_sl"):
                    cond = "WHERE p.base_profile_name = 'reversal_probability_sl' OR p.request_id = 3"
                else:
                    cond = "WHERE p.base_profile_name = ?"
                    params = (base_profile,)
            else:
                cond = "WHERE 1=1"
        elif profile == "runtime":
            table = "positions"
            cond = "WHERE p.order_status = 'FILLED'"
            params = ()
        elif profile and profile != "main":
            table = "profile_positions"
            if profile in ("all_profiles", "all"):
                cond = "WHERE p.order_status = 'FILLED'"
                params = ()
            else:
                cond = "WHERE p.profile_name = ? AND p.order_status = 'FILLED'"
                params = (profile,)
        else:
            table = "paper_positions" if is_rd else "positions"
            cond = "WHERE p.order_status = 'FILLED'"
            params = ()
        sql = f"""
            SELECT p.*, m.question
            FROM {table} p
            LEFT JOIN markets m ON p.market_id = m.market_id
            {cond}
            ORDER BY p.closed_at DESC
            LIMIT 100000
        """
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

        # Build targeted resolution memory map only for positions that exited early (e.g. STOP_LOSS)
        market_resolutions = {}
        needed_mids = list(set([
            r["market_id"] for r in rows 
            if r["close_reason"] not in ('RESOLUTION', 'EXPIRED_ORACLE', 'SETTLED', 'ORACLE_SETTLED', 'FINAL_RESOLUTION')
        ]))
        if needed_mids:
            for i in range(0, len(needed_mids), 500):
                chunk = needed_mids[i:i+500]
                placeholders = ",".join("?" * len(chunk))
                try:
                    res_cursor = await db.execute(f"""
                        SELECT market_id, side, exit_price, realised_pnl
                        FROM profile_positions INDEXED BY idx_profile_positions_market
                        WHERE market_id IN ({placeholders})
                          AND close_reason IN ('RESOLUTION', 'EXPIRED_ORACLE', 'SETTLED', 'ORACLE_SETTLED', 'FINAL_RESOLUTION')
                    """, chunk)
                except Exception:
                    res_cursor = await db.execute(f"""
                        SELECT market_id, side, exit_price, realised_pnl
                        FROM profile_positions
                        WHERE market_id IN ({placeholders})
                          AND close_reason IN ('RESOLUTION', 'EXPIRED_ORACLE', 'SETTLED', 'ORACLE_SETTLED', 'FINAL_RESOLUTION')
                    """, chunk)
                res_rows = await res_cursor.fetchall()
                for r in res_rows:
                    m_id = r[0]
                    if m_id not in market_resolutions:
                        s, ep, pnl = r[1], r[2], r[3]
                        if ep >= 0.80 or (pnl is not None and pnl > 0):
                            market_resolutions[m_id] = s
                        elif ep <= 0.20 or (pnl is not None and pnl < 0):
                            market_resolutions[m_id] = "NO" if s == "YES" else "YES"

            still_missing = [m for m in needed_mids if m not in market_resolutions]
            if still_missing:
                for tbl in ("paper_positions", "positions"):
                    for i in range(0, len(still_missing), 500):
                        chunk = still_missing[i:i+500]
                        placeholders = ",".join("?" * len(chunk))
                        p_cursor = await db.execute(f"""
                            SELECT market_id, side, exit_price, realised_pnl
                            FROM {tbl}
                            WHERE market_id IN ({placeholders})
                              AND close_reason IN ('RESOLUTION', 'EXPIRED_ORACLE', 'SETTLED', 'ORACLE_SETTLED', 'FINAL_RESOLUTION')
                        """, chunk)
                        p_rows = await p_cursor.fetchall()
                        for r in p_rows:
                            m_id = r[0]
                            if m_id not in market_resolutions:
                                s, ep, pnl = r[1], r[2], r[3]
                                if ep >= 0.80 or (pnl is not None and pnl > 0):
                                    market_resolutions[m_id] = s
                                elif ep <= 0.20 or (pnl is not None and pnl < 0):
                                    market_resolutions[m_id] = "NO" if s == "YES" else "YES"
        
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
            if d.get("streak_depth") is None:
                notes_str = d.get("notes")
                if notes_str and "streak_depth" in notes_str:
                    try:
                        parsed_notes = json.loads(notes_str)
                        d["streak_depth"] = parsed_notes.get("streak_depth")
                    except Exception:
                        pass
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
                
            sym = d.get("symbol")
            if not sym:
                q = (d.get("question") or "").lower()
                if "btc" in q or "bitcoin" in q: sym = "BTC"
                elif "eth" in q or "ethereum" in q: sym = "ETH"
                elif "sol" in q or "solana" in q: sym = "SOL"
                elif "xrp" in q or "ripple" in q: sym = "XRP"
            d["symbol"] = sym or ""

            if symbol:
                if (d.get("symbol") or "").upper() != symbol.upper(): continue
                
            if side:
                if (d.get("side") or "").upper() != side.upper(): continue

            if window_minutes is not None:
                if d.get("window_minutes") != window_minutes: continue

            if streak_depth is not None:
                if d.get("streak_depth") != streak_depth: continue
                
            # Max Drawdown Filter
            if max_drawdown_val is not None:
                ep = d.get("entry_price")
                min_seen = d.get("min_price_seen")
                if ep and ep > 0 and min_seen is not None:
                    max_dd = ((ep - min_seen) / ep) * 100.0
                    op = max_drawdown_op or "gt"
                    if op == "gt" and max_dd < max_drawdown_val:
                        continue
                    elif op == "lt" and max_dd >= max_drawdown_val:
                        continue
                    elif op == "eq" and abs(max_dd - max_drawdown_val) > 0.1:
                        continue
                else:
                    continue

            # Entry Price Filter
            ep = d.get("entry_price")
            if ep is not None:
                if entry_price_min is not None and entry_price_max is not None:
                    if not (entry_price_min <= ep <= entry_price_max):
                        continue
                elif entry_price_min is not None:
                    if ep < entry_price_min:
                        continue
                elif entry_price_max is not None:
                    if ep > entry_price_max:
                        continue
                elif entry_price_op in ("between", "range", "band"):
                    min_v = entry_price_val if entry_price_val is not None else 0.0
                    max_v = entry_price_max if entry_price_max is not None else 1.0
                    if not (min_v <= ep <= max_v):
                        continue
                elif entry_price_val is not None:
                    op = entry_price_op or "eq"
                    if op == "gt" and ep < entry_price_val:
                        continue
                    elif op == "lt" and ep >= entry_price_val:
                        continue
                    elif op == "eq" and abs(ep - entry_price_val) > 0.0001:
                        continue
            elif entry_price_val is not None or entry_price_min is not None or entry_price_max is not None:
                continue

            # Time Remaining (Seconds Left at Entry) Filter
            stc = d.get("seconds_to_close_at_entry")
            if stc is not None:
                try:
                    stc_val = float(stc)
                    if time_remaining_min is not None and time_remaining_max is not None:
                        if not (time_remaining_min <= stc_val <= time_remaining_max):
                            continue
                    elif time_remaining_min is not None:
                        if stc_val < time_remaining_min:
                            continue
                    elif time_remaining_max is not None:
                        if stc_val > time_remaining_max:
                            continue
                    elif time_remaining_op in ("between", "range", "band"):
                        min_v = time_remaining_val if time_remaining_val is not None else 0.0
                        max_v = time_remaining_max if time_remaining_max is not None else 999999.0
                        if not (min_v <= stc_val <= max_v):
                            continue
                    elif time_remaining_val is not None:
                        op = time_remaining_op or "eq"
                        if op == "gt" and stc_val < time_remaining_val:
                            continue
                        elif op == "lt" and stc_val >= time_remaining_val:
                            continue
                        elif op == "eq" and abs(stc_val - time_remaining_val) > 0.5:
                            continue
                except (ValueError, TypeError):
                    pass
            elif time_remaining_val is not None or time_remaining_min is not None or time_remaining_max is not None:
                continue

            # Exit Price Filter
            if exit_price_val is not None:
                xp = d.get("exit_price")
                if xp is not None:
                    op = exit_price_op or "eq"
                    if op == "gt" and xp < exit_price_val:
                        continue
                    elif op == "lt" and xp >= exit_price_val:
                        continue
                    elif op == "eq" and abs(xp - exit_price_val) > 0.0001:
                        continue
                else:
                    continue

            # Calculate drawdown percentages from entry for UI rendering
            ep = d.get("entry_price")
            min_seen = d.get("min_price_seen")
            max_seen = d.get("max_price_seen")
            
            d["min_drawdown"] = None
            d["max_drawdown"] = None
            
            if ep and ep > 0:
                if min_seen is not None:
                    d["max_drawdown"] = ((ep - min_seen) / ep) * 100.0
                if max_seen is not None:
                    d["min_drawdown"] = ((ep - max_seen) / ep) * 100.0

            # Compute Hold-to-Resolution (HTR) side-by-side trade fields
            m_id = d.get("market_id")
            t_side = d.get("side")
            size = float(d.get("size_usdc") or 0.0)
            shares = float(d.get("shares_bought") or 0.0)
            act_pnl = float(d.get("realised_pnl") or 0.0)
            reason = d.get("close_reason") or ""

            if profile == "executioner" or reason in ("RESOLUTION", "EXPIRED_ORACLE", "SETTLED", "ORACLE_SETTLED", "FINAL_RESOLUTION"):
                htr_win = (act_pnl > 0)
                htr_pnl = act_pnl
                winning_side = t_side if htr_win else ("NO" if t_side == "YES" else "YES")
            else:
                winning_side = market_resolutions.get(m_id)
                if winning_side:
                    htr_win = (winning_side == t_side)
                    htr_pnl = round(shares * 1.0 - size, 4) if htr_win else round(-size, 4)
                else:
                    htr_win = (act_pnl > 0)
                    htr_pnl = act_pnl

            d["htr_winning_side"] = winning_side
            d["htr_is_win"] = htr_win
            d["htr_pnl"] = htr_pnl

            if reason == "STOP_LOSS":
                if htr_win:
                    d["htr_status"] = "WHIPSAW"
                else:
                    d["htr_status"] = "RESCUED"
            else:
                d["htr_status"] = "HELD_TO_RES"

            results.append(d)

        total_count = len(results)
        total_wins = sum(1 for t in results if (t.get("realised_pnl") or 0.0) > 0.0)
        total_pnl = sum((t.get("realised_pnl") or 0.0) for t in results)

        htr_total_wins = sum(1 for t in results if t.get("htr_is_win"))
        htr_total_pnl = sum((t.get("htr_pnl") or 0.0) for t in results)
        whipsaw_count = sum(1 for t in results if t.get("htr_status") == "WHIPSAW")
        rescued_count = sum(1 for t in results if t.get("htr_status") == "RESCUED")

        act_win_rate = round((total_wins / total_count * 100) if total_count > 0 else 0, 1)
        htr_win_rate = round((htr_total_wins / total_count * 100) if total_count > 0 else 0, 1)

        if limit and limit > 0:
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paginated_trades = results[start_idx:end_idx]
            total_pages = (total_count + limit - 1) // limit
        else:
            paginated_trades = results
            total_pages = 1

        use_htr = bool(htr_mode == 1)

        chart_series = [
            {
                "position_id": t.get("position_id"),
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
                "created_at": t.get("created_at"),
                "realised_pnl": t.get("htr_pnl") if use_htr else t.get("realised_pnl"),
                "side": t.get("side"),
                "question": t.get("question"),
                "market_id": t.get("market_id"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price")
            }
            for t in results
        ]

        return {
            "trades": paginated_trades,
            "chart_series": chart_series,
            "total": total_count,
            "total_wins": htr_total_wins if use_htr else total_wins,
            "total_pnl": round(htr_total_pnl if use_htr else total_pnl, 4),
            "act_total_wins": total_wins,
            "act_total_pnl": round(total_pnl, 4),
            "act_win_rate": act_win_rate,
            "htr_total_wins": htr_total_wins,
            "htr_total_pnl": round(htr_total_pnl, 4),
            "htr_win_rate": htr_win_rate,
            "whipsaw_count": whipsaw_count,
            "rescued_count": rescued_count,
            "htr_mode": htr_mode,
            "page": page,
            "limit": limit,
            "pages": total_pages
        }
    finally:
        await db.close()

class CustomQueryRequest(BaseModel):
    query: str
    env: Optional[str] = None
    limit: Optional[int] = 5000

async def execute_custom_query(query_str: str, env: Optional[str] = None, limit: int = 5000) -> Dict[str, Any]:
    cleaned = (query_str or "").strip()
    if not cleaned:
        return {
            "success": False,
            "error": "Query cannot be empty",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }

    # Strip SQL comments
    import re
    cleaned_no_comments = re.sub(r'(--[^\n]*|\/\*.*?\*\/)', '', cleaned, flags=re.DOTALL).strip()
    if not cleaned_no_comments:
        return {
            "success": False,
            "error": "Query contains no executable SQL statements",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }

    # Strict check: must start with SELECT or WITH
    first_word = cleaned_no_comments.split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        return {
            "success": False,
            "error": "Security Restriction: Only read-only queries (SELECT / WITH) are permitted.",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }

    # Check for forbidden mutation keywords
    forbidden_pattern = r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b'
    if re.search(forbidden_pattern, cleaned_no_comments, re.IGNORECASE):
        return {
            "success": False,
            "error": "Security Restriction: Database mutation statements are strictly forbidden.",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }

    # Enforce single statement
    stmt_body = cleaned_no_comments.rstrip(';').strip()
    if ';' in stmt_body:
        return {
            "success": False,
            "error": "Security Restriction: Multiple SQL statements separated by semicolons are not permitted.",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }

    db = await _db_ro()
    start_t = time.perf_counter()
    try:
        cursor = await db.execute(stmt_body)
        raw_rows = await cursor.fetchall()
    except Exception as e:
        return {
            "success": False,
            "error": f"Database Error: {str(e)}",
            "trades": [],
            "chart_series": [],
            "total": 0,
            "total_wins": 0,
            "total_pnl": 0.0,
            "act_win_rate": 0.0
        }
    finally:
        await db.close()

    elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)

    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    tz_et = ZoneInfo("America/New_York")
    tz_ist = ZoneInfo("Asia/Kolkata")

    results = []
    max_rows = limit if limit and limit > 0 else 5000

    for idx, r in enumerate(raw_rows[:max_rows]):
        d = dict(r)

        # Fallback position_id if missing
        pos_id = d.get("position_id")
        if not pos_id:
            pos_id = d.get("market_id") or d.get("signal_id") or f"row_{idx+1}"
            d["position_id"] = str(pos_id)

        # Streak depth extraction from notes if missing
        if d.get("streak_depth") is None:
            notes_str = d.get("notes")
            if notes_str and "streak_depth" in str(notes_str):
                try:
                    parsed_notes = json.loads(notes_str)
                    d["streak_depth"] = parsed_notes.get("streak_depth")
                except Exception:
                    pass

        opened_at = d.get("opened_at")
        closed_at = d.get("closed_at")

        if opened_at:
            try:
                dt_utc_open = datetime.fromtimestamp(float(opened_at), tz=timezone.utc)
                d["opened_at_et"] = dt_utc_open.astimezone(tz_et).strftime("%Y-%m-%d %H:%M:%S ET")
                d["opened_at_ist"] = dt_utc_open.astimezone(tz_ist).strftime("%Y-%m-%d %H:%M:%S IST")
            except Exception:
                d["opened_at_et"] = "—"
                d["opened_at_ist"] = "—"
        else:
            d["opened_at_et"] = "—"
            d["opened_at_ist"] = "—"

        if closed_at:
            try:
                dt_utc_close = datetime.fromtimestamp(float(closed_at), tz=timezone.utc)
                d["closed_at_et"] = dt_utc_close.astimezone(tz_et).strftime("%Y-%m-%d %H:%M:%S ET")
                d["closed_at_ist"] = dt_utc_close.astimezone(tz_ist).strftime("%Y-%m-%d %H:%M:%S IST")
            except Exception:
                d["closed_at_et"] = "—"
                d["closed_at_ist"] = "—"
        else:
            d["closed_at_et"] = "—"
            d["closed_at_ist"] = "—"

        sym = d.get("symbol")
        if not sym:
            q = (d.get("question") or "").lower()
            if "btc" in q or "bitcoin" in q: sym = "BTC"
            elif "eth" in q or "ethereum" in q: sym = "ETH"
            elif "sol" in q or "solana" in q: sym = "SOL"
            elif "xrp" in q or "ripple" in q: sym = "XRP"
        d["symbol"] = sym or ""

        # Drawdown calculation
        ep = d.get("entry_price")
        min_seen = d.get("min_price_seen")
        max_seen = d.get("max_price_seen")
        d["min_drawdown"] = None
        d["max_drawdown"] = None
        if ep and float(ep) > 0:
            ep_val = float(ep)
            if min_seen is not None:
                d["max_drawdown"] = ((ep_val - float(min_seen)) / ep_val) * 100.0
            if max_seen is not None:
                d["min_drawdown"] = ((ep_val - float(max_seen)) / ep_val) * 100.0

        pnl = float(d.get("realised_pnl") or 0.0)
        d["htr_pnl"] = d.get("htr_pnl", pnl)
        d["htr_is_win"] = d.get("htr_is_win", (pnl > 0))
        d["htr_status"] = d.get("htr_status", "HELD_TO_RES")

        results.append(d)

    total_count = len(results)
    total_wins = sum(1 for t in results if (float(t.get("realised_pnl") or 0.0) > 0.0))
    total_pnl = round(sum(float(t.get("realised_pnl") or 0.0) for t in results), 4)
    act_win_rate = round((total_wins / total_count * 100) if total_count > 0 else 0.0, 1)

    chart_series = [
        {
            "position_id": t.get("position_id"),
            "opened_at": t.get("opened_at"),
            "closed_at": t.get("closed_at"),
            "created_at": t.get("created_at"),
            "realised_pnl": t.get("realised_pnl"),
            "side": t.get("side"),
            "question": t.get("question"),
            "market_id": t.get("market_id"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price")
        }
        for t in results
    ]

    return {
        "success": True,
        "trades": results,
        "chart_series": chart_series,
        "total": total_count,
        "total_wins": total_wins,
        "total_pnl": total_pnl,
        "act_total_wins": total_wins,
        "act_total_pnl": total_pnl,
        "act_win_rate": act_win_rate,
        "htr_total_wins": total_wins,
        "htr_total_pnl": total_pnl,
        "htr_win_rate": act_win_rate,
        "whipsaw_count": 0,
        "rescued_count": 0,
        "pages": 1,
        "page": 1,
        "elapsed_ms": elapsed_ms,
        "truncated": len(raw_rows) > max_rows
    }

PROFILE_TWIN_MAP = {
    "streak_momentum": "reversal",
    "reversal": "streak_momentum",
    "stop_loss": "reversal_stop_loss",
    "reversal_stop_loss": "stop_loss",
    "stop_loss_filtered": "reversal_stop_loss_filtered",
    "reversal_stop_loss_filtered": "stop_loss_filtered",
    "probability_sl": "reversal_probability_sl",
    "reversal_probability_sl": "probability_sl",

    "claude_good": "claude_good_reversal",
    "claude_good_reversal": "claude_good",
    "claude_gated": "claude_gated_reversal",
    "claude_gated_reversal": "claude_gated",
    "claude_audit": "claude_audit_reversal",
    "claude_audit_reversal": "claude_audit",
}

async def _fetch_single_pnl_series(db, profile: Optional[str], is_rd: bool) -> List[Dict]:
    if profile == "executioner":
        table = "executioner_positions"
        cond = "WHERE order_status = 'FILLED'"
        params = ()
    elif profile == "runtime":
        table = "positions"
        cond = "WHERE order_status = 'FILLED'"
        params = ()
    elif profile and profile != "main":
        table = "profile_positions"
        cond = "WHERE profile_name = ? AND order_status = 'FILLED'"
        params = (profile,)
    else:
        table = "paper_positions" if (is_rd or _is_paper()) else "positions"
        cond = "WHERE order_status = 'FILLED'"
        params = ()
    sql = f"SELECT closed_at, realised_pnl FROM {table} {cond} ORDER BY closed_at ASC"
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    running = 0.0
    series = []
    for row in rows:
        running += row["realised_pnl"] or 0
        series.append({"ts": row["closed_at"], "pnl": round(running, 4)})
    return series

async def get_pnl_series(is_rd: bool = False, profile: Optional[str] = None, mode: Optional[str] = "trend") -> Any:
    db = await _db_ro()
    try:
        req_profile = profile or "main"
        mode_lower = (mode or "trend").lower()

        # Handle merged comparison mode
        if mode_lower == "merged" and req_profile in PROFILE_TWIN_MAP:
            twin_profile = PROFILE_TWIN_MAP[req_profile]
            # Determine which is trend vs reversal
            if req_profile.endswith("_reversal") or req_profile in ("reversal", "reversal_stop_loss", "reversal_stop_loss_filtered", "reversal_probability_sl"):
                p_rev = req_profile
                p_trend = twin_profile
            else:
                p_trend = req_profile
                p_rev = twin_profile

            trend_series = await _fetch_single_pnl_series(db, p_trend, is_rd)
            reversal_series = await _fetch_single_pnl_series(db, p_rev, is_rd)
            return {
                "mode": "merged",
                "trend_profile": p_trend,
                "reversal_profile": p_rev,
                "trend_series": trend_series,
                "reversal_series": reversal_series
            }
        
        # Handle explicit reversal mode
        if mode_lower == "reversal":
            target_p = PROFILE_TWIN_MAP.get(req_profile, req_profile)
            return await _fetch_single_pnl_series(db, target_p, is_rd)

        # Standard trend mode
        return await _fetch_single_pnl_series(db, req_profile, is_rd)
    finally:
        await db.close()

async def get_reversal_status(is_rd: bool = False) -> Dict[str, Any]:
    db = await _db()
    try:
        from unittest.mock import MagicMock, AsyncMock
        table = "paper_positions" if is_rd else "positions"
        
        # On-demand resolve any unresolved positions whose market closed in the past
        try:
            from src.strategy.resolver import market_resolver
            from src.db.queries import get_market
            from src.strategy.math_utils import realised_pnl as calc_realised_pnl, is_mock_market_id
            import os
            
            cursor = await db.execute(
                f"SELECT position_id, market_id, side, size_usdc, shares_bought, order_status, notes FROM {table} "
                f"WHERE realised_pnl IS NULL"
            )
            unresolved = await cursor.fetchall()
            now_ts = int(time.time())
            
            gamma_client = None
            if _engine_ref and hasattr(_engine_ref, "gamma"):
                gamma_client = _engine_ref.gamma
            elif _engine_ref and hasattr(_engine_ref, "position_mgr") and hasattr(_engine_ref.position_mgr, "gamma"):
                gamma_client = _engine_ref.position_mgr.gamma
                
            if unresolved and gamma_client:
                for pos in unresolved:
                    m_id = pos["market_id"]
                    if is_mock_market_id(m_id) and ("PYTEST_CURRENT_TEST" not in os.environ):
                        continue
                    m_data = await get_market(db, m_id)
                    if m_data and m_data.get("closes_at", 0) <= now_ts:
                        closes_at = m_data.get("closes_at", 0)
                        q = m_data.get("question", "").lower()
                        slug = m_data.get("slug", "").lower()
                        duration = pos.get("window_minutes") or (15 if ("15m" in slug or "15m" in q or "15-min" in q) else 5)
                        coin = "btc" if ("bitcoin" in q or "btc" in q) else "eth"
                        start_time = closes_at - duration * 60
                        side = pos["side"]
                        
                        res = await market_resolver.get_market_resolution(
                            db, gamma_client, m_id, coin, duration, start_time, side
                        )
                        if res:
                            outcome, exit_price = res
                            pnl = calc_realised_pnl(exit_price, pos["shares_bought"], pos["size_usdc"])
                            actual_close_time = closes_at if closes_at > 0 else now_ts
                            
                            # Log audit trails
                            updated_notes = pos["notes"] or "{}"
                            try:
                                notes_data = json.loads(updated_notes)
                                notes_data["eff_exit"] = round(exit_price, 4)
                                notes_data["total_p"] = round(pos["shares_bought"] * exit_price, 4)
                                updated_notes = json.dumps(notes_data)
                            except: pass
                            
                            await db.execute(
                                f"UPDATE {table} SET order_status = 'FILLED', closed_at = ?, exit_price = ?, realised_pnl = ?, close_reason = ?, notes = ? WHERE position_id = ?",
                                (actual_close_time, exit_price, pnl, "RESOLUTION", updated_notes, pos["position_id"])
                            )
                            await db.commit()
                            logger.info("dashboard_resolved_unresolved_on_demand", position_id=pos["position_id"], pnl=pnl)
        except Exception as res_err:
            logger.error("failed_dashboard_on_demand_resolutions", error=str(res_err))

        lookback_window = 86400
        if _config_ref:
            lookback_window = getattr(_config_ref, "reversal_lookback_window_seconds", 86400)
            if isinstance(lookback_window, (MagicMock, AsyncMock)):
                lookback_window = 86400
        min_closed_at = int(time.time()) - lookback_window

        # Query last 10 completed positions closed within the lookback window
        cursor = await db.execute(
            f"SELECT notes, realised_pnl FROM {table} "
            f"WHERE order_status = 'FILLED' AND realised_pnl IS NOT NULL "
            f"AND closed_at >= ? "
            f"ORDER BY closed_at DESC LIMIT 10",
            (min_closed_at,)
        )
        rows = await cursor.fetchall()
        
        if not rows:
            return {
                "mode": "TREND",
                "win_rate": 0.0,
                "consecutive_losses": 0,
                "completed_count": 0,
                "is_active": False
            }
        
        switch_threshold = 0.50
        if _config_ref:
            switch_threshold = getattr(_config_ref, "reversal_switch_threshold", 0.50)
            if isinstance(switch_threshold, (MagicMock, AsyncMock)):
                switch_threshold = 0.50
            
        last_notes = rows[0]["notes"]
        last_strategy = "TREND"
        if last_notes and last_notes.startswith("{"):
            try:
                last_notes_parsed = json.loads(last_notes)
                if isinstance(last_notes_parsed, dict):
                    last_strategy = last_notes_parsed.get("strategy", "TREND")
            except Exception:
                pass

        # Consecutive losses
        consecutive_losses = 0
        for r in rows:
            if r["realised_pnl"] < 0:
                consecutive_losses += 1
            else:
                break
                
        # Win rate
        wins = sum(1 for r in rows if r["realised_pnl"] > 0)
        win_rate = wins / len(rows)
        
        # Calculate mode (logic matches _get_active_strategy_mode)
        if len(rows) >= 5 and all(r["realised_pnl"] < 0 for r in rows[:5]):
            mode = "REVERSAL"
        elif len(rows) < 10:
            mode = last_strategy
        else:
            if win_rate < switch_threshold:
                mode = "REVERSAL" if last_strategy == "TREND" else "TREND"
            else:
                mode = last_strategy
                
        return {
            "mode": mode,
            "win_rate": round(win_rate * 100, 1),
            "consecutive_losses": consecutive_losses,
            "completed_count": len(rows),
            "is_active": mode == "REVERSAL"
        }
    finally:
        await db.close()

# ─────────────────────────────────────────────
# Runtime Config
# ─────────────────────────────────────────────
DEFAULT_RUNTIME_CONFIG = {
    "position_size_usdc": 2.0,
    "max_daily_loss_usdc": 75.0,
    "max_total_exposure_usdc": 50.0,
    "min_ev_threshold": 0.02,
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
    "min_entry_price": 0.30,
    "max_entry_price": 0.60,
    "min_persistence_score": 0.55,
    "lossmomentum_skip_enabled": True,
    "reversal_toggling_enabled": True,
    "reversal_lookback_n": 10,
    "reversal_switch_threshold": 0.45,
    "reversal_spread_penalty": 0.0,
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
async def api_stats(request: Request, type: str = None, profile: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    return await get_stats(is_rd=is_rd, profile=profile)

@app.get("/api/positions")
@app.get("/rd/api/positions")
async def api_positions(request: Request, type: str = None, profile: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    return await get_positions(is_rd=is_rd, profile=profile)

@app.get("/api/history")
@app.get("/rd/api/history")
async def api_history(request: Request, limit: int = 50, type: str = None, profile: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    return await get_history(limit, is_rd=is_rd, profile=profile)

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
    page: int = 1,
    type: str = None,
    env: Optional[str] = None,
    max_drawdown_op: Optional[str] = None,
    max_drawdown_val: Optional[float] = None,
    entry_price_op: Optional[str] = None,
    entry_price_val: Optional[float] = None,
    entry_price_min: Optional[float] = None,
    entry_price_max: Optional[float] = None,
    time_remaining_op: Optional[str] = None,
    time_remaining_val: Optional[float] = None,
    time_remaining_min: Optional[float] = None,
    time_remaining_max: Optional[float] = None,
    exit_price_op: Optional[str] = None,
    exit_price_val: Optional[float] = None,
    profile: Optional[str] = None,
    base_profile: Optional[str] = None,
    htr_mode: Optional[int] = 0,
    _: str = Depends(verify_key)
):
    is_rd = _is_paper()
    # Explicit override via query parameters
    effective_env = env or type
    if effective_env == "live":
        is_rd = False
    elif effective_env in ["paper", "rd"]:
        is_rd = True

    return await get_filtered_history(
        is_rd=is_rd,
        day=day,
        hour_et=hour_et,
        symbol=symbol,
        side=side,
        window_minutes=window_minutes,
        streak_depth=streak_depth,
        limit=limit,
        page=page,
        max_drawdown_op=max_drawdown_op,
        max_drawdown_val=max_drawdown_val,
        entry_price_op=entry_price_op,
        entry_price_val=entry_price_val,
        entry_price_min=entry_price_min,
        entry_price_max=entry_price_max,
        time_remaining_op=time_remaining_op,
        time_remaining_val=time_remaining_val,
        time_remaining_min=time_remaining_min,
        time_remaining_max=time_remaining_max,
        exit_price_op=exit_price_op,
        exit_price_val=exit_price_val,
        profile=profile,
        base_profile=base_profile,
        htr_mode=htr_mode
    )

@app.post("/api/custom-query")
@app.post("/rd/api/custom-query")
async def api_custom_query(
    request: Request,
    req_data: CustomQueryRequest,
    _: str = Depends(verify_key)
):
    return await execute_custom_query(
        query_str=req_data.query,
        env=req_data.env,
        limit=req_data.limit or 5000
    )


# ─────────────────────────────────────────────
# Profile Notes (editable per-profile context)
# ─────────────────────────────────────────────
_CMH_DEFAULT_NOTES: Dict[str, str] = {
    "cross_market_hedge": (
        "Buys opposite sides of correlated BTC+ETH contracts expiring at the same time. "
        "One leg wins big when prices diverge. Low WR (~13%) but large individual wins. "
        "Triggers when BTC and ETH YES/NO prices are both below 0.40."
    ),
}


@app.get("/api/pnl-series")
@app.get("/rd/api/pnl-series")
async def api_pnl_series(request: Request, type: str = None, profile: Optional[str] = None, mode: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    q_profile = profile or request.query_params.get("profile")
    q_mode = mode or request.query_params.get("mode") or "trend"
    return await get_pnl_series(is_rd=is_rd, profile=q_profile, mode=q_mode)

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
    if update.key in [
        "hold_to_resolution",
        "trailing_tp_enabled",
        "ghost_guard_enabled",
        "_TESTING_only_15min_windows",
        "lossmomentum_skip_enabled",
        "reversal_toggling_enabled"
    ]:
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
async def api_close_position(position_id: str, profile: Optional[str] = None, _: str = Depends(verify_key)):
    if not _engine_ref or not getattr(_engine_ref, 'position_mgr', None):
        raise HTTPException(status_code=400, detail="Engine not initialized")
    if profile and profile != "main":
        db = await _db()
        try:
            cursor = await db.execute("SELECT * FROM profile_positions WHERE position_id = ?", (position_id,))
            pos = await cursor.fetchone()
            if not pos:
                raise HTTPException(status_code=404, detail="Profile position not found")
            pos = dict(pos)
            side = pos.get("side", "NO")
            target_token_id = pos.get("target_token_id")
            exit_price = 0.5
            if _engine_ref.position_mgr.ws:
                price_cache = _engine_ref.position_mgr.ws.price_cache
                bid_cache = _engine_ref.position_mgr.ws.bid_cache
                ask_cache = _engine_ref.position_mgr.ws.ask_cache
                if target_token_id:
                    exit_price = bid_cache.get(target_token_id) or price_cache.get(target_token_id) or ask_cache.get(target_token_id) or 0.5
            from src.strategy.math_utils import calc_realised_pnl
            pnl = calc_realised_pnl(exit_price, pos["shares_bought"], pos["size_usdc"])
            from src.db.queries import close_profile_position
            await close_profile_position(db, position_id, exit_price, pnl, "MANUAL_CLOSE")
            return {"status": "ok", "message": f"Manual closure triggered for profile position {position_id}"}
        finally:
            await db.close()
    else:
        await _engine_ref.position_mgr.manual_close_position(position_id)
        return {"status": "ok", "message": f"Closure triggered for {position_id}"}

@app.post("/api/history/clear")
@app.post("/rd/api/history/clear")
async def api_clear_history(request: Request, type: str = None, profile: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    db = await _db()
    try:
        if profile and profile != "main":
            await db.execute("DELETE FROM profile_positions WHERE profile_name = ? AND order_status IN ('CLOSED', 'REJECTED', 'CANCELLED', 'FILLED')", (profile,))
        else:
            table = "paper_positions" if (is_rd or _is_paper()) else "positions"
            await db.execute(f"DELETE FROM {table} WHERE order_status IN ('CLOSED', 'REJECTED', 'CANCELLED', 'FILLED')")
        await db.commit()
        return {"status": "ok", "message": "History cleared"}
    finally:
        await db.close()

@app.delete("/api/history/{position_id}")
@app.delete("/rd/api/history/{position_id}")
async def api_delete_history(request: Request, position_id: str, type: str = None, profile: Optional[str] = None, _: str = Depends(verify_key)):
    is_rd = _is_paper()
    db = await _db()
    try:
        if profile and profile != "main":
            await db.execute("DELETE FROM profile_positions WHERE position_id = ? AND profile_name = ?", (position_id, profile))
        else:
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
    
    profile = websocket.query_params.get("profile", "main")
    is_rd = _is_paper()
    await manager.connect(websocket)

    last_stats_refresh = 0.0
    last_positions_refresh = 0.0
    cached_stats = {}
    cached_positions = []

    try:
        while True:
            try:
                now_ts = time.time()

                # Refresh DB stats every 5s and DB open positions every 2s
                if now_ts - last_stats_refresh >= 5.0 or not cached_stats:
                    cached_stats = await get_stats(is_rd=is_rd, profile=profile)
                    last_stats_refresh = now_ts

                if now_ts - last_positions_refresh >= 2.0 or not cached_positions:
                    cached_positions = await get_positions(is_rd=is_rd, profile=profile)
                    last_positions_refresh = now_ts

                stats = cached_stats
                positions = [dict(p) for p in cached_positions]
                now = int(now_ts)
                
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
                        if shares_rem <= 0.0 and p.get("order_status") in ("OPEN", "PENDING"):
                            shares_rem = float(p.get("shares_bought", 0.0))
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

                persistence_scores = {
                    "BTC": {"up": 0.0, "down": 0.0, "overall": 0.0},
                    "ETH": {"up": 0.0, "down": 0.0, "overall": 0.0}
                }
                if _engine_ref and hasattr(_engine_ref, "decision") and _engine_ref.decision:
                    tracker = getattr(_engine_ref.decision, "regime_tracker", None)
                    if tracker:
                        for _asset in ("BTC", "ETH"):
                            _sd = tracker.get_current_scores_dict(_asset)
                            persistence_scores[_asset] = {
                                "up": _sd.get("p_up", 0.0),
                                "down": _sd.get("p_down", 0.0),
                                "overall": _sd.get("overall", 0.0)
                            }

                # Query reversal status
                reversal_status = await get_reversal_status(is_rd=is_rd)

                # Send update to frontend only if connected
                if websocket.client_state.value == 1: # CONNECTED
                    await websocket.send_json({
                        "type": "update",
                        "stats": stats,
                        "positions": positions,
                        "candles": candles,
                        "streaks": streaks,
                        "persistence_scores": persistence_scores,
                        "reversal_status": reversal_status,
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
                err_msg = str(e)
                if (
                    "Disconnect" in err_name or
                    "ConnectionClosed" in err_name or
                    "ConnectionReset" in err_name or
                    "ClientDisconnected" in err_name or
                    "close message" in err_msg or
                    "already closed" in err_msg
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
        err_msg = str(e)
        if (
            "Disconnect" in err_name or
            "ConnectionClosed" in err_name or
            "ConnectionReset" in err_name or
            "ClientDisconnected" in err_name or
            "close message" in err_msg or
            "already closed" in err_msg
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
    if page.exists():
        headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        return HTMLResponse(page.read_text(), headers=headers)
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)

@app.get("/rd", response_class=HTMLResponse)
async def serve_rd_dashboard():
    rd = dashboard_dir / "rd.html"
    if rd.exists():
        headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        return HTMLResponse(rd.read_text(), headers=headers)
    return HTMLResponse("<h1>R&D Dashboard not found</h1>", status_code=404)

async def start_dashboard_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
