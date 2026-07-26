"""Error log: problems go to a file, never to the screen.

Anything written to stderr while curses is drawing lands on top of the
frame and stays there, so the package logger gets a file handler and
``propagate = False``. Without that, ``logging.lastResort`` prints every
WARNING to stderr, straight across the dashboard.

Nothing here is load-bearing: if the file cannot be opened, logging is
silenced and the dashboard runs as normal. Refusing to start over a log
file would be a worse bug than a missing log.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

# Configure the *package* logger, not the root logger: this tool must not
# reconfigure logging for anything that imports it, and keeping the handler
# here (with propagate=False) is what guarantees no record can travel up to
# a root handler and out to stderr.
PACKAGE_LOGGER = __name__.split(".")[0]

ENV_LOG_PATH = "ZENDURE_MQTT_VIEWER_LOG"
LOG_FILENAME = "error.log"

# Rotation keeps a hub that reconnects in a loop all night from filling the
# disk, while still leaving enough history to see what started it.
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 2

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Handlers this module installed, so repeated configure() calls replace
# them instead of stacking up duplicate lines per record.
_installed: list[logging.Handler] = []
_active_path: Optional[Path] = None


def active_path() -> Optional[Path]:
    """The file records are going to, or None if they are going nowhere."""
    return _active_path


def default_log_path() -> Path:
    """Where the log goes unless told otherwise.

    Same directory as the last-seen cache: one place to look for everything
    this tool writes.
    """
    override = os.environ.get(ENV_LOG_PATH)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "zendure-mqtt-viewer" / LOG_FILENAME


def resolve_log_path(cli_path: Optional[str] = None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser()
    return default_log_path()


def _reset(logger: logging.Logger) -> None:
    for handler in _installed:
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    _installed.clear()


def _silence(logger: logging.Logger) -> None:
    """No file, but still no stderr: swallow records instead of leaking them."""
    handler = logging.NullHandler()
    logger.addHandler(handler)
    _installed.append(handler)
    logger.propagate = False


def configure(path: Optional[Path], level: int = logging.WARNING) -> Optional[Path]:
    """Send this package's log records to ``path``. Returns the path in use.

    ``None`` path (or an unusable one) means "log nowhere", and returns
    None. Either way the caller gets a logger that cannot write to the
    terminal.
    """
    global _active_path

    logger = logging.getLogger(PACKAGE_LOGGER)
    _reset(logger)
    logger.setLevel(level)
    _active_path = None
    # A failing handler otherwise prints "--- Logging error ---" plus a
    # traceback to stderr, which is exactly the mess this module exists to
    # prevent.
    logging.raiseExceptions = False

    if path is None:
        _silence(logger)
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,  # do not create the file until something goes wrong
        )
    except OSError:
        _silence(logger)
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(level)
    logger.addHandler(handler)
    _installed.append(handler)
    logger.propagate = False
    _active_path = path
    return path
