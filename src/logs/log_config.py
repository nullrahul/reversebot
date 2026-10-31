"""
Structured logging configuration for Polymarket Reverse Bot.
Uses structlog with JSON output — every line includes:
timestamp (ISO8601 UTC), version, module, level, market_id, event, message.
Daily rotation, 30-day retention, simultaneous stdout + file output.
"""

import os
import sys
import logging
import logging.handlers
from pathlib import Path

import structlog
import asyncio

DASHBOARD_CALLBACK = None  # Set by dashboard.py
_ACTIVE_BOT_VERSION = "v0.0.0"


def version_processor(logger, method_name, event_dict):
    """
    Ensure loggers created at import time with default 'v0.0.0' automatically
    report the active bot_version configured in setup_logging().
    """
    if _ACTIVE_BOT_VERSION != "v0.0.0" and event_dict.get("version") in (None, "v0.0.0"):
        event_dict["version"] = _ACTIVE_BOT_VERSION
    return event_dict


def dashboard_processor(logger, method_name, event_dict):
    """
    Structlog processor that forwards log events to the web dashboard.
    """
    if DASHBOARD_CALLBACK:
        # Clone dict to avoid mutation issues
        data = dict(event_dict)
        msg = data.pop("event", "no_event")
        level = method_name
        detail = data.get("message", "")
        
        # We use call_soon_threadsafe or similar if needed, 
        # but usually structlog runs in the main thread's loop context here
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    DASHBOARD_CALLBACK(level, msg, detail, data),
                    loop
                )
        except Exception:
            pass
            
    return event_dict


class SafeStreamHandler(logging.StreamHandler):
    """
    A StreamHandler that ignores BrokenPipeError.
    Useful when running in background/screen where the terminal may detach.
    """
    def emit(self, record):
        try:
            super().emit(record)
        except (BrokenPipeError, AttributeError):
            # Ignore broken pipes or cases where sys.stdout is replaced/removed
            pass


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    bot_version: str = "v0.0.0",
) -> None:
    """
    Configure structlog with JSON rendering, rotating file handler,
    and stdout output.
    """
    global _ACTIVE_BOT_VERSION
    if bot_version and bot_version != "v0.0.0":
        _ACTIVE_BOT_VERSION = bot_version

    # Ensure log directory exists
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Map string level to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # --- Standard library logging setup (for file + stdout) ---
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # SILENCE NOISE: Force third-party libraries to INFO or WARNING level
    # This prevents aiosqlite from logging every SQL query in DEBUG mode.
    logging.getLogger("aiosqlite").setLevel(logging.INFO)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.INFO)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Formatter for stdlib handlers (structlog will handle JSON formatting)
    formatter = logging.Formatter("%(message)s")

    # File handler: rotating 10MB file, keeps only 1 backup (max 2 files)
    # This satisfies the user's request for "single or two log files"
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path / "reversebot.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Hardened Stdout handler: prevents crash on BrokenPipeError
    stdout_handler = SafeStreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    _configure_structlog()


def _configure_structlog() -> None:
    """Configure structlog processor pipeline with version_processor and JSON output."""
    structlog.configure(
        processors=[
            # Add log level
            structlog.stdlib.add_log_level,
            # Add logger name
            structlog.stdlib.add_logger_name,
            # Add timestamp in ISO8601 UTC
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Add stack info for exceptions
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Add default fields
            structlog.processors.UnicodeDecoder(),
            # Inject active bot version for module-level loggers
            version_processor,
            # Forward to dashboard
            dashboard_processor,
            # Render to JSON
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# Configure structlog at import time so module-level get_logger().bind(...) calls
# before setup_logging() still attach version_processor and JSONRenderer.
_configure_structlog()


def get_logger(module: str, bot_version: str = "v0.0.0") -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger bound with module name and bot version.
    Every log line automatically includes these fields.
    """
    resolved_version = bot_version if bot_version != "v0.0.0" else _ACTIVE_BOT_VERSION
    return structlog.get_logger(module).bind(
        version=resolved_version,
        module=module,
    )
