"""
Configuration for Tick Recorder.
"""

import os

# Snapshot interval (seconds)
RECORDING_INTERVAL_SECS = 2.0

# Shared memory state paths
SHM_TMP_PATH = "/dev/shm/reversebot_state.tmp"
SHM_FINAL_PATH = "/dev/shm/reversebot_state.json"

# Fallback path if /dev/shm is not available (e.g. non-Linux/Mac local testing)
FALLBACK_SHM_DIR = os.path.expanduser("~/.reversebot_shm")
FALLBACK_TMP_PATH = os.path.join(FALLBACK_SHM_DIR, "reversebot_state.tmp")
FALLBACK_FINAL_PATH = os.path.join(FALLBACK_SHM_DIR, "reversebot_state.json")

# Storage directory
TICKS_DB_DIR = os.path.expanduser("~/reversebot/data/ticks")

# Recorded assets
RECORDED_SYMBOLS = ["BTC", "ETH", "SOL", "XRP"]

# Buffer & Flush Settings
FLUSH_INTERVAL_SECS = 30.0
STALE_THRESHOLD_SECS = 10.0
