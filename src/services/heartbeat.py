"""Hardening: heartbeat writer used by the Docker healthcheck.

The bot touches ``data/.heartbeat`` on a fixed cadence. If the polling loop
is wedged, no touches happen and the file's mtime goes stale, which makes
the Docker healthcheck fail and forces a container restart.
"""

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Path lives in the ``data`` volume so it survives container restarts and
# is visible to the host-side ``docker`` healthcheck command.
HEARTBEAT_PATH = Path(os.getenv("BOT_HEARTBEAT_PATH", "data/.heartbeat"))


def write_heartbeat() -> None:
    """Refresh the heartbeat file's mtime. Safe to call from any job."""
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()
    except Exception as exc:
        # Never let a heartbeat failure crash the bot.
        logger.warning("Failed to write heartbeat file %s: %s", HEARTBEAT_PATH, exc)
