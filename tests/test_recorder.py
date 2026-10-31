"""
Unit tests for Tick Recorder module (state_exporter, state_reader, db_writer, main).
"""

import json
import os
import tempfile
import pytest
from unittest.mock import MagicMock

from src.recorder.state_exporter import StateExporter
from src.recorder.state_reader import StateReader
from src.recorder.db_writer import TickDBWriter

@pytest.mark.asyncio
async def test_state_exporter_and_reader():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = os.path.join(tmpdir, "state.tmp")
        final_path = os.path.join(tmpdir, "state.json")

        exporter = StateExporter()
        exporter._tmp_path = tmp_path
        exporter._final_path = final_path

        scanner_mock = MagicMock()
        scanner_mock.config = MagicMock()
        scanner_mock.config.BOT_VERSION = "v4.50"
        scanner_mock.tracked_markets = {
            "0xmarket1": {
                "market_id": "0xmarket1",
                "symbol": "BTC",
                "window_minutes": 5,
                "closes_at": 1781881200,
                "yes_price": 0.55,
                "no_price": 0.45,
                "yes_token_id": "y1",
                "no_token_id": "n1"
            }
        }
        scanner_mock.ws = MagicMock()
        scanner_mock.ws.price_cache = {"y1": 0.55, "n1": 0.45}
        scanner_mock.ws.bid_cache = {"y1": 0.54}
        scanner_mock.ws.ask_cache = {"y1": 0.56}
        scanner_mock.ws.book_cache = {}
        scanner_mock._spot_ticks = {"BTC": [(1000.0, 95000.0)]}

        await exporter.export_state_snapshot(scanner_mock)

        assert os.path.exists(final_path)

        reader = StateReader(override_path=final_path)
        snapshot, status = reader.read_snapshot()

        assert status == "OK"
        assert snapshot is not None
        assert "markets" in snapshot
        assert "0xmarket1" in snapshot["markets"]
        assert snapshot["markets"]["0xmarket1"]["yes_price"] == 0.55

def test_db_writer():
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = TickDBWriter(db_dir=tmpdir)
        today_str = writer._get_today_str()
        db_path = writer.get_db_path(today_str)

        snapshots = [{
            "timestamp_ms": 1700000000000,
            "market_id": "0xmarket1",
            "symbol": "BTC",
            "window_minutes": 5,
            "closes_at": 1700000300,
            "yes_price": 0.55,
            "no_price": 0.45,
            "cvd_60s": 15000.0,
            "book_imbalance": -0.25
        }]

        ext_ticks = [{
            "timestamp_ms": 1700000000000,
            "symbol": "BTC",
            "spot_price": 95000.0,
            "cvd_60s": 15000.0,
            "cvd_5m": 45000.0
        }]

        writer.write_batch(snapshots, ext_ticks)
        writer.close()

        assert os.path.exists(db_path)
