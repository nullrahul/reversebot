"""
Tick Recorder Service Main Loop — High-frequency market tape capture process.
Runs independently under PM2 without affecting core trading bot loops.
"""

import asyncio
import signal
import sys
import time
from typing import List, Dict, Any

from src.logs.log_config import get_logger
from src.recorder.config import (
    RECORDING_INTERVAL_SECS,
    FLUSH_INTERVAL_SECS,
    RECORDED_SYMBOLS,
)
from src.recorder.db_writer import TickDBWriter
from src.recorder.state_reader import StateReader

logger = get_logger("recorder_main")

class TickRecorderService:
    def __init__(self, interval: float = RECORDING_INTERVAL_SECS, flush_interval: float = FLUSH_INTERVAL_SECS):
        self.interval = interval
        self.flush_interval = flush_interval
        self.reader = StateReader()
        self.writer = TickDBWriter()
        self.is_running = False
        
        self.buffered_snapshots: List[Dict[str, Any]] = []
        self.buffered_ext_ticks: List[Dict[str, Any]] = []
        
        self.last_ts_ms = 0
        self.last_flush_time = time.time()
        self.gap_start_ms = 0

    async def start(self):
        """Start recording loop."""
        self.is_running = True
        logger.info("tick_recorder_service_starting", interval=self.interval, flush_interval=self.flush_interval)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:
                pass

        while self.is_running:
            start_loop = time.time()
            try:
                self._record_step()
            except Exception as e:
                logger.error("recorder_loop_error", error=str(e))

            # Flush periodically
            if (time.time() - self.last_flush_time) >= self.flush_interval:
                self._flush()

            elapsed = time.time() - start_loop
            sleep_secs = max(0.01, self.interval - elapsed)
            await asyncio.sleep(sleep_secs)

        self._flush()
        self.writer.close()
        logger.info("tick_recorder_service_stopped")

    def _record_step(self):
        snapshot, status = self.reader.read_snapshot()
        now_ms = int(time.time() * 1000)

        if status != "OK" or snapshot is None:
            if self.gap_start_ms == 0:
                self.gap_start_ms = now_ms
            return

        # Closed gap if previously open
        if self.gap_start_ms > 0:
            self.writer.log_gap(self.gap_start_ms, now_ms, reason=status)
            self.gap_start_ms = 0

        ts_ms = snapshot.get("timestamp_ms", now_ms)
        if ts_ms <= self.last_ts_ms:
            return  # No new state written since last check

        self.last_ts_ms = ts_ms

        # Buffer market snapshots
        markets = snapshot.get("markets", {})
        for mid, mdata in markets.items():
            mdata_copy = dict(mdata)
            mdata_copy["timestamp_ms"] = ts_ms
            self.buffered_snapshots.append(mdata_copy)

        # Buffer external ticks (BTC, ETH, SOL, XRP)
        ext = snapshot.get("external", {})
        for sym in RECORDED_SYMBOLS:
            edata = ext.get(sym, {})
            self.buffered_ext_ticks.append({
                "timestamp_ms": ts_ms,
                "symbol": sym,
                "spot_price": edata.get("spot_price", 0.0),
                "cvd_60s": edata.get("cvd_60s", 0.0),
                "cvd_5m": edata.get("cvd_5m", 0.0),
                "spot_velocity_10s": edata.get("spot_velocity_10s", 0.0),
                "realized_vol_5m": edata.get("realized_vol_5m", 0.0),
                "realized_vol_1h": edata.get("realized_vol_1h", 0.0),
            })

    def _flush(self):
        if self.buffered_snapshots or self.buffered_ext_ticks:
            self.writer.write_batch(self.buffered_snapshots, self.buffered_ext_ticks)
            self.buffered_snapshots.clear()
            self.buffered_ext_ticks.clear()
        self.last_flush_time = time.time()

    def stop(self):
        logger.info("tick_recorder_service_stopping")
        self.is_running = False

def main():
    service = TickRecorderService()
    asyncio.run(service.start())

if __name__ == "__main__":
    main()
