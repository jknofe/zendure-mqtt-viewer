"""Last-seen value cache.

The report stream is delta-only and some fields are broadcast rarely -
packState, minSoc, socSet, packNum, the firmware versions. A freshly started
process therefore shows "--" for them until the hub happens to send one,
which can be many minutes. This module writes the last known values to a
small JSON file on exit (and periodically) and loads them back at startup so
the dashboard resumes where it left off instead of from scratch.

Restored values keep their original timestamps, so they are labelled with
their true age and dimmed as stale - the cache fills the blind window, it
does not pretend to be live data.

Nothing here is load-bearing: every failure path degrades to "no cache",
never to an error the user has to deal with. A dashboard that starts with
empty fields is a minor annoyance; one that refuses to start because a cache
file is corrupt is a bug.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .state import DashboardState

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ENV_CACHE_PATH = "ZENDURE_MQTT_VIEWER_CACHE"

# Values older than this are not worth restoring - a week-old SoC reading is
# noise, and showing it (however dimmed) is worse than showing "--".
MAX_AGE_SECONDS = 24 * 3600


def default_cache_path() -> Path:
    override = os.environ.get(ENV_CACHE_PATH)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "zendure-mqtt-viewer" / "last-seen.json"


def resolve_cache_path(cli_path: Optional[str] = None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser()
    return default_cache_path()


def save(state: DashboardState, path: Path, now: Optional[float] = None) -> bool:
    """Write the state's last-seen values. True if written.

    Writes to a temp file in the same directory and renames it into place, so
    an interrupted write (Ctrl-C, power loss) can never leave a half-written
    cache that the next start would have to cope with.
    """
    snapshot = state.to_snapshot()
    if not snapshot.get("hub") and not snapshot.get("packs"):
        return False  # nothing learned this run; keep whatever is on disk

    payload = {
        "version": SCHEMA_VERSION,
        "saved_at": now if now is not None else time.time(),
        "state": snapshot,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".last-seen-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, path)
        except BaseException:
            # Includes KeyboardInterrupt - do not leave the temp file behind.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("could not save last-seen cache to %s: %s", path, exc)
        return False
    return True


def load(state: DashboardState, path: Path, now: Optional[float] = None) -> int:
    """Merge cached last-seen values into ``state``. Returns fields restored.

    Returns 0 for every "no usable cache" case - missing file, unreadable
    file, bad JSON, wrong schema version, or data old enough to be useless.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("ignoring corrupt last-seen cache %s: %s", path, exc)
        return 0

    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return 0

    saved_at = payload.get("saved_at")
    if isinstance(saved_at, (int, float)):
        age = (now if now is not None else time.time()) - saved_at
        if age > MAX_AGE_SECONDS:
            logger.info("last-seen cache is %.1fh old, ignoring", age / 3600)
            return 0

    snapshot = payload.get("state")
    if not isinstance(snapshot, dict):
        return 0
    return state.restore_snapshot(snapshot)
