"""
Logging setup shared by the CLI, the scheduler, the MCP server and job workers.

Library code uses plain ``logging.getLogger(__name__)``; entry points call
``configure_logging()`` once.  The level is also exported through the
``SIIMPLI_LOG_LEVEL`` environment variable so job worker processes (which are
separate Python processes) log at the same level.
"""
import logging
import os
import sys
from typing import Optional, TextIO

LOG_LEVEL_ENV = "SIIMPLI_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Chatty third-party loggers that drown out job progress at INFO/DEBUG
_QUIET_LOGGERS = ("apscheduler", "urllib3", "asyncio", "httpx", "httpcore")


def configure_logging(level: Optional[str] = None, stream: Optional[TextIO] = None) -> None:
    """Configure root logging to ``stream`` (default stderr).  Safe to call repeatedly.

    ``level`` falls back to ``$SIIMPLI_LOG_LEVEL`` then ``INFO``.  Logs go to
    stderr so they never mix with CLI tables on stdout or an MCP stdio stream.
    """
    name = (level or os.environ.get(LOG_LEVEL_ENV) or "INFO").upper()
    numeric = logging.getLevelName(name)
    if not isinstance(numeric, int):
        numeric, name = logging.INFO, "INFO"
    os.environ[LOG_LEVEL_ENV] = name  # inherited by job worker processes

    root = logging.getLogger()
    root.setLevel(numeric)
    handler = next((h for h in root.handlers if getattr(h, "_siimpli", False)), None)
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler._siimpli = True
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(handler)
    elif stream is not None:
        handler.setStream(stream)
    handler.setLevel(numeric)

    for noisy in _QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """Get a logger by name (kept for callers of the original stub)."""
    return logging.getLogger(name)
