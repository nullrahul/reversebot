"""
Tests for dashboard API filters, including persistency query parameters.
"""

import os
import sys
import tempfile
import pytest
import pytest_asyncio
import aiosqlite
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.queries import init_db, close_db, create_position, update_position_status, close_position, upsert_market, update_position_min_max_prices
from src.api.dashboard import get_filtered_history, execute_custom_query

@pytest_asyncio.fixture
async def temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_conn = await init_db(tmp.name)
    yield db_conn, tmp.name
    await close_db(db_conn)
    os.unlink(tmp.name)

def _sample_market(market_id: str) -> dict:
    return {
        "market_id": market_id,
        "question": f"Will {market_id} go up?",
        "category": "Crypto",
        "yes_price": 0.5,
        "no_price": 0.5,
        "yes_volume_24h": 1000.0,
        "total_volume": 10000.0,
        "resolution_source": "oracle",
        "closes_at": int(time.time()) + 3600,
        "is_flagged": 0,
        "flag_reason": None,
        "last_seen_above_threshold": None,
        "consecutive_cycles_above": 0,
    }

@pytest.mark.asyncio
async def test_get_filtered_history_drawdown(temp_db, monkeypatch):
    db, db_path = temp_db
    
    # Monkeypatch the database path retrieval in dashboard
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)
    monkeypatch.setattr("src.api.dashboard._is_paper", lambda: True)

    # Insert markets to satisfy foreign key constraint
    await upsert_market(db, _sample_market("market_1"))
    await upsert_market(db, _sample_market("market_2"))

    # Insert closed trades
    # 1. Trade 1: Entry = 0.5, Min price seen = 0.4 (Drawdown = 20.0%)
    pid1 = await create_position(db, "market_1", 0.5, 10.0, 20.0, "v3.94", paper=True)
    await update_position_min_max_prices(db, pid1, 0.4, 0.6, paper=True)
    await update_position_status(db, pid1, "OPEN", paper=True)
    await close_position(db, pid1, 0.6, 2.0, "TAKE_PROFIT", paper=True)

    # 2. Trade 2: Entry = 0.5, Min price seen = 0.45 (Drawdown = 10.0%)
    pid2 = await create_position(db, "market_2", 0.5, 10.0, 20.0, "v3.94", paper=True)
    await update_position_min_max_prices(db, pid2, 0.45, 0.55, paper=True)
    await update_position_status(db, pid2, "OPEN", paper=True)
    await close_position(db, pid2, 0.4, -2.0, "STOP_LOSS", paper=True)

    # Test filtering all (no filter)
    res_all_dict = await get_filtered_history(is_rd=True)
    res_all = res_all_dict["trades"]
    assert len(res_all) == 2
    assert res_all_dict["total"] == 2

    # Test filtering drawdown >= 15% (gt 15.0)
    res_gt_dict = await get_filtered_history(
        is_rd=True,
        max_drawdown_op="gt",
        max_drawdown_val=15.0
    )
    res_gt = res_gt_dict["trades"]
    assert len(res_gt) == 1
    assert res_gt[0]["position_id"] == pid1

    # Test filtering drawdown < 15% (lt 15.0)
    res_lt_dict = await get_filtered_history(
        is_rd=True,
        max_drawdown_op="lt",
        max_drawdown_val=15.0
    )
    res_lt = res_lt_dict["trades"]
    assert len(res_lt) == 1
    assert res_lt[0]["position_id"] == pid2

    # Test filtering drawdown == 20% (eq 20.0)
    res_eq_dict = await get_filtered_history(
        is_rd=True,
        max_drawdown_op="eq",
        max_drawdown_val=20.0
    )
    res_eq = res_eq_dict["trades"]
    assert len(res_eq) == 1
    assert res_eq[0]["position_id"] == pid1


@pytest.mark.asyncio
async def test_api_history_filtered_env_override(temp_db, monkeypatch):
    db, db_path = temp_db
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)
    monkeypatch.setattr("src.api.dashboard._is_paper", lambda: True) # Default is paper mode

    from src.api.dashboard import api_history_filtered
    from unittest.mock import MagicMock

    # Insert market
    await upsert_market(db, _sample_market("market_env"))

    # Create one live trade and one paper trade
    pid_live = await create_position(db, "market_env", 0.5, 10.0, 20.0, "v3.94", paper=False, persistence_score=0.60)
    await update_position_status(db, pid_live, "OPEN", paper=False)
    await close_position(db, pid_live, 0.6, 2.0, "TAKE_PROFIT", paper=False)

    pid_paper = await create_position(db, "market_env", 0.5, 10.0, 20.0, "v3.94", paper=True, persistence_score=0.52)
    await update_position_status(db, pid_paper, "OPEN", paper=True)
    await close_position(db, pid_paper, 0.4, -2.0, "STOP_LOSS", paper=True)

    # 1. Default behaviour (respects monkeypatched _is_paper = True, i.e. paper_positions)
    res_default_dict = await api_history_filtered(request=MagicMock(), _="mock_key")
    res_default = res_default_dict["trades"]
    assert len(res_default) == 1
    assert res_default[0]["position_id"] == pid_paper

    # 2. Override to live
    res_live_dict = await api_history_filtered(request=MagicMock(), env="live", _="mock_key")
    res_live = res_live_dict["trades"]
    assert len(res_live) == 1
    assert res_live[0]["position_id"] == pid_live

    # 3. Override to paper
    res_paper_dict = await api_history_filtered(request=MagicMock(), env="paper", _="mock_key")
    res_paper = res_paper_dict["trades"]
    assert len(res_paper) == 1
    assert res_paper[0]["position_id"] == pid_paper


@pytest.mark.asyncio
async def test_get_filtered_history_htr_mode(temp_db, monkeypatch):
    db, db_path = temp_db
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)
    monkeypatch.setattr("src.api.dashboard._is_paper", lambda: True)

    await upsert_market(db, _sample_market("market_htr"))

    # Create position that was stopped out but market resolved to YES
    pid = await create_position(db, "market_htr", 0.60, 10.0, 16.66, "v3.94", paper=True)
    await update_position_status(db, pid, "OPEN", paper=True)
    await close_position(db, pid, 0.54, -1.0, "STOP_LOSS", paper=True)

    # 1. Fetch with htr_mode=0 (Actual SL)
    res_sl = await get_filtered_history(is_rd=True, htr_mode=0)
    assert res_sl["total"] == 1
    assert res_sl["act_total_pnl"] == -1.0
    assert "htr_pnl" in res_sl["trades"][0]

    # 2. Fetch with htr_mode=1 (HTR Sim)
    res_htr = await get_filtered_history(is_rd=True, htr_mode=1)
    assert res_htr["total"] == 1
    assert res_htr["htr_mode"] == 1
    assert "whipsaw_count" in res_htr


@pytest.mark.asyncio
async def test_runtime_profile_dashboard_endpoints(temp_db, monkeypatch):
    db, db_path = temp_db
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)
    monkeypatch.setattr("src.api.dashboard._is_paper", lambda: True) # Server in paper mode, but runtime queries live

    from src.api.dashboard import get_stats, get_positions, get_history, get_pnl_series

    await upsert_market(db, _sample_market("market_live_1"))
    await upsert_market(db, _sample_market("market_live_2"))

    # Insert a live filled trade and a live open position in positions table (paper=False)
    pid_live_1 = await create_position(db, "market_live_1", 0.06, 1.02, 17.0, "v3.94", paper=False)
    await update_position_status(db, pid_live_1, "OPEN", paper=False)
    await close_position(db, pid_live_1, 1.0, 15.68, "RESOLUTION", paper=False)

    pid_live_2 = await create_position(db, "market_live_2", 0.05, 1.05, 21.0, "v3.94", paper=False)
    await update_position_status(db, pid_live_2, "OPEN", paper=False)

    # 1. get_stats with profile="runtime"
    stats = await get_stats(profile="runtime")
    assert stats["is_paper"] is False
    assert stats["total_closed"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 0
    assert stats["total_pnl"] == 15.68
    assert stats["open_count"] == 1

    # 2. get_positions with profile="runtime"
    positions = await get_positions(profile="runtime")
    assert len(positions) == 1
    assert positions[0]["position_id"] == pid_live_2

    # 3. get_history with profile="runtime"
    history = await get_history(limit=10, profile="runtime")
    assert len(history) == 1
    assert history[0]["position_id"] == pid_live_1

    # 4. get_filtered_history with profile="runtime"
    filtered = await get_filtered_history(profile="runtime")
    assert filtered["total"] == 1
    assert filtered["trades"][0]["position_id"] == pid_live_1

    # 5. get_pnl_series with profile="runtime"
    series = await get_pnl_series(profile="runtime")
    assert len(series) == 1
    assert series[0]["pnl"] == 15.68


@pytest.mark.asyncio
async def test_execute_custom_query_valid(temp_db, monkeypatch):
    db, db_path = temp_db
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)

    await upsert_market(db, _sample_market("market_sql_1"))
    pid = await create_position(db, "market_sql_1", 0.45, 10.0, 22.22, "v3.94", paper=True)
    await update_position_status(db, pid, "OPEN", paper=True)
    await close_position(db, pid, 1.0, 12.22, "RESOLUTION", paper=True)

    # Valid SELECT
    res = await execute_custom_query("SELECT * FROM paper_positions WHERE order_status = 'FILLED'")
    assert res["success"] is True
    assert res["total"] == 1
    assert res["total_wins"] == 1
    assert res["total_pnl"] == 12.22
    assert res["act_win_rate"] == 100.0
    assert len(res["trades"]) == 1
    assert res["trades"][0]["position_id"] == pid
    assert len(res["chart_series"]) == 1


@pytest.mark.asyncio
async def test_execute_custom_query_security_checks(temp_db, monkeypatch):
    db, db_path = temp_db
    monkeypatch.setattr("src.api.dashboard._get_db_path", lambda: db_path)

    # 1. Empty query
    res = await execute_custom_query("   ")
    assert res["success"] is False
    assert "Query cannot be empty" in res["error"]

    # 2. DROP TABLE
    res = await execute_custom_query("DROP TABLE paper_positions")
    assert res["success"] is False
    assert "Only read-only queries" in res["error"] or "forbidden" in res["error"]

    # 3. Mutation keyword in SELECT
    res = await execute_custom_query("SELECT * FROM paper_positions; DELETE FROM paper_positions")
    assert res["success"] is False

    # 4. Incomplete syntax
    res = await execute_custom_query("SELECT * FROM WHERE")
    assert res["success"] is False
    assert "Database Error" in res["error"]





