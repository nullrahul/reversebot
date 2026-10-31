import pytest
import os
import tempfile
import json
import sqlite3
import asyncio
from src.strategy.router.weights import RouterWeightsLoader
from src.strategy.router.scorer import RouterScorer
from src.strategy.router.policy import RouterPolicy
from src.strategy.router.logger import ShadowRouterLogger

@pytest.fixture
def temp_weights_config():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        data = {
            "weights_version": "v4.5.4-three-layer-architecture",
            "router_version": "v4.5.0-test",
            "feature_schema_version": "v1.0",
            "profile_allowlist": [
                "cheap_reversal", "reversal_yes_cvd_imb", "reversal_yes_cvd_imb_wide",
                "reversal_stop_loss", "reversal_eth_5m_momentum", "micro_flow_no",
                "cross_market_hedge", "single_market_hedge_expensive"
            ],
            "engines": {
                "Reversal": {"weights": {"w_price": 0.40, "w_cvd": 0.30, "w_imbalance": 0.30}, "trigger_threshold": 0.70},
                "Loss_Recovery": {"weights": {"w_price": 0.50, "w_cvd": 0.25, "w_post_loss": 0.25}, "trigger_threshold": 0.65},
                "Micro_Flow": {"weights": {"w_side_no": 0.50, "w_discount_price": 0.30, "w_time_close": 0.20}, "trigger_threshold": 0.75}
            },
            "policy": {
                "score_margin_min": 0.08,
                "gray_zone": {
                    "noise_floor": 0.60,
                    "watch_zone_ceiling": 0.70
                }
            }
        }
        json.dump(data, f)
        f_path = f.name

    yield f_path

    if os.path.exists(f_path):
        os.remove(f_path)

@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".db") as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shadow_router_logs (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id             TEXT DEFAULT (lower(hex(randomblob(16)))),
            signal_id               TEXT,
            market_id               TEXT NOT NULL,
            timestamp               DATETIME DEFAULT CURRENT_TIMESTAMP,
            router_version          TEXT NOT NULL DEFAULT 'v4.5.0-shadow',
            weights_version         TEXT NOT NULL DEFAULT 'v4.5.1',
            feature_schema_version TEXT NOT NULL DEFAULT 'v1.0',
            entry_price             REAL,
            cvd_60s                 REAL,
            book_imbalance          REAL,
            streak_depth            INTEGER,
            seconds_to_close        REAL,
            asset                   TEXT,
            legacy_decision         TEXT NOT NULL,
            router_decision         TEXT NOT NULL,
            winning_engine          TEXT NOT NULL,
            winning_score           REAL NOT NULL,
            second_best_score       REAL DEFAULT 0.0,
            score_margin            REAL DEFAULT 0.0,
            reason_rejected         TEXT,
            market_regime           TEXT,
            conviction_level        TEXT,
            raw_scores_json         TEXT,
            resolved_at             DATETIME,
            legacy_pnl              REAL,
            realized_router_pnl     REAL,
            delta_pnl               REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_positions (
            position_id TEXT PRIMARY KEY,
            market_id TEXT,
            realised_pnl REAL,
            closed_at DATETIME
        )
    """)
    conn.commit()
    conn.close()

    yield db_path

    if os.path.exists(db_path):
        os.remove(db_path)


def test_router_weights_loader(temp_weights_config):
    loader = RouterWeightsLoader(config_path=temp_weights_config)
    assert loader.weights_version == "v4.5.4-three-layer-architecture"
    assert loader.score_margin_min == 0.08
    assert loader.get_engine_threshold("Reversal") == 0.70


def test_malformed_config_validation():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        f.write("{ invalid json")
        invalid_path = f.name

    try:
        with pytest.raises(ValueError, match="Malformed JSON"):
            RouterWeightsLoader(config_path=invalid_path)
    finally:
        if os.path.exists(invalid_path):
            os.remove(invalid_path)


def test_router_scorer_deterministic(temp_weights_config):
    loader = RouterWeightsLoader(config_path=temp_weights_config)
    scorer = RouterScorer(loader)

    features = {
        "entry_price": 0.55,
        "cvd_60s": -15000.0,
        "book_imbalance": -0.20,
        "side": "YES"
    }

    scores = scorer.calculate_scores(features)
    assert "Reversal" in scores
    assert scores["Reversal"] == 1.00


def test_router_policy_gray_zone_and_tie_breaking(temp_weights_config):
    loader = RouterWeightsLoader(config_path=temp_weights_config)
    policy = RouterPolicy(loader)

    # Case 1: Noise Floor (< 0.60)
    decision, reason, win_score, second_score, margin = policy.evaluate_policy({
        "Reversal": 0.50,
        "Loss_Recovery": 0.30,
        "Micro_Flow": 0.20
    })
    assert decision == "REJECT"
    assert "NoiseFloor" in reason

    # Case 2: Watch Zone (0.60 <= Score < 0.70)
    decision, reason, win_score, second_score, margin = policy.evaluate_policy({
        "Reversal": 0.65,
        "Loss_Recovery": 0.30,
        "Micro_Flow": 0.20
    })
    assert decision == "WATCH"
    assert "WatchZone" in reason

    # Case 3: Conflicted Regime (Delta S < 0.08)
    decision, reason, win_score, second_score, margin = policy.evaluate_policy({
        "Reversal": 0.80,
        "Loss_Recovery": 0.75,
        "Micro_Flow": 0.20
    })
    assert decision == "REJECT"
    assert "ConflictedRegime" in reason
    assert margin == 0.05

    # Case 4: High Conviction Accept (Score >= 0.70 & Margin >= 0.08)
    decision, reason, win_score, second_score, margin = policy.evaluate_policy({
        "Reversal": 0.85,
        "Loss_Recovery": 0.50,
        "Micro_Flow": 0.20
    })
    assert decision == "ACCEPT"
    assert "HighConviction" in reason
    assert margin == 0.35


@pytest.mark.asyncio
async def test_shadow_router_logger_and_reconciliation(temp_weights_config, temp_db):
    loader = RouterWeightsLoader(config_path=temp_weights_config)
    logger_inst = ShadowRouterLogger(db_path=temp_db, weights_loader=loader)

    signal_payload = {
        "market_id": "test_m_100",
        "signal_id": "sig_100",
        "asset": "BTC",
        "entry_price": 0.55,
        "cvd_60s": -15000.0,
        "book_imbalance": -0.25,
        "streak_depth": 3,
        "seconds_to_close": 120.0,
        "side": "YES"
    }

    res = await logger_inst.evaluate_and_log(signal_payload, legacy_decision="ACCEPTED")
    assert res["router_decision"] == "ACCEPT"
    assert "eval_ms" in res

    # Verify database insertion
    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()
    cur.execute("SELECT market_id, router_decision, winning_engine, winning_score FROM shadow_router_logs WHERE market_id = 'test_m_100'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "test_m_100"
    assert row[1] == "ACCEPT"
    assert row[2] == "Reversal"

    # Insert position resolution in profile_positions to test reconciliation
    cur.execute("INSERT INTO profile_positions (position_id, market_id, realised_pnl, closed_at) VALUES ('pos_100', 'test_m_100', 10.50, datetime('now'))")
    conn.commit()
    conn.close()

    # Run decoupled outcome reconciliation
    updated_count = logger_inst.reconcile_shadow_outcomes()
    assert updated_count == 1

    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()
    cur.execute("SELECT legacy_pnl, realized_router_pnl FROM shadow_router_logs WHERE market_id = 'test_m_100'")
    rec_row = cur.fetchone()
    conn.close()

    assert rec_row[0] == 10.50
    assert rec_row[1] == 10.50


@pytest.mark.asyncio
async def test_profile_allowlist_filtering(temp_weights_config, temp_db):
    loader = RouterWeightsLoader(config_path=temp_weights_config)
    logger_inst = ShadowRouterLogger(db_path=temp_db, weights_loader=loader)

    # Signal from non-allowlisted profile (e.g. loss_skip)
    payload_bad = {
        "market_id": "m_bad_1",
        "signal_id": "sig_bad_1",
        "profile_name": "loss_skip",
        "entry_price": 0.55
    }
    res_bad = await logger_inst.evaluate_and_log(payload_bad, legacy_decision="ACCEPTED")
    assert res_bad["router_decision"] == "REJECT"
    assert "NotOnAllowlist" in res_bad["reason_rejected"]

    # Signal from allowlisted profile (e.g. cheap_reversal)
    payload_good = {
        "market_id": "m_good_1",
        "signal_id": "sig_good_1",
        "profile_name": "cheap_reversal",
        "entry_price": 0.35,
        "cvd_60s": -15000.0,
        "book_imbalance": -0.25,
        "side": "YES"
    }
    res_good = await logger_inst.evaluate_and_log(payload_good, legacy_decision="ACCEPTED")
    assert res_good["router_decision"] == "ACCEPT"

