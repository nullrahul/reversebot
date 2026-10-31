"""
State Reader — Reads shared-memory JSON snapshot written by the bot.
"""

import json
import os
import time
from typing import Optional, Dict, Any, Tuple

from src.recorder.config import (
    SHM_FINAL_PATH,
    FALLBACK_FINAL_PATH,
    STALE_THRESHOLD_SECS,
)

class StateReader:
    def __init__(self, override_path: Optional[str] = None):
        self.override_path = override_path

    def _get_target_path(self) -> str:
        if self.override_path:
            return self.override_path
        if os.path.exists(SHM_FINAL_PATH):
            return SHM_FINAL_PATH
        return FALLBACK_FINAL_PATH

    def read_snapshot(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Reads JSON snapshot from shared memory.
        Returns (snapshot_dict, status_code).
        Status codes: 'OK', 'FILE_NOT_FOUND', 'CORRUPT_JSON', 'STALE_SNAPSHOT'.
        """
        path = self._get_target_path()
        if not os.path.exists(path):
            return None, "FILE_NOT_FOUND"

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None, "CORRUPT_JSON"

        ts_ms = data.get("timestamp_ms", 0)
        now_ms = int(time.time() * 1000)
        if (now_ms - ts_ms) > (STALE_THRESHOLD_SECS * 1000):
            return data, "STALE_SNAPSHOT"

        return data, "OK"
