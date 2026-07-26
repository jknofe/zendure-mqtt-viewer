"""--replay mode: feed a capture file through the exact same decode+state
path as live mode, with no network involved at all.

This is deliberately dumb: it reuses DashboardState.apply_payload, the same
function the live MQTT callback calls, so replay and live can never drift
apart in behavior.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from .state import DashboardState, MalformedMessageError, parse_line


def replay_file(
    path: Path,
    state: DashboardState,
    *,
    speed: float = 0.0,
    on_line: Optional[Callable[[DashboardState], None]] = None,
) -> None:
    """Feed each line of a capture file into ``state``.

    speed=0 (default): replay as fast as possible, no artificial delay.
    speed>0: pace playback using the gaps between consecutive payload
    ``timestamp`` values, divided by ``speed`` (speed=1.0 -> real time,
    speed=10.0 -> 10x fast-forward). Capped at 5s per gap so a capture with
    a multi-hour silent stretch doesn't hang a demo.
    """
    prev_ts: Optional[float] = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                payload = parse_line(line)
            except MalformedMessageError as exc:
                state.note_parse_error(str(exc))
            else:
                ts = payload.get("timestamp")
                if speed > 0 and isinstance(ts, (int, float)) and prev_ts is not None:
                    gap = (ts - prev_ts) / speed
                    if gap > 0:
                        time.sleep(min(gap, 5.0))
                if isinstance(ts, (int, float)):
                    prev_ts = ts
                state.apply_payload(payload, time.time())
            if on_line is not None:
                on_line(state)
