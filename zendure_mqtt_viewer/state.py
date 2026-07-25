"""In-memory dashboard state: delta-merge, staleness tracking, undecoded capture.

Also pure / I/O-free (aside from json.loads on an already-received string),
so it is fully unit-testable. This is the piece that encodes the single most
important design constraint from PROTOCOL.md: the MQTT stream is delta-only,
so a field that hasn't appeared in a message is *unknown*, not zero, and must
keep showing its last known value with an age indicator.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Optional

from . import decode


class MalformedMessageError(ValueError):
    """Raised when a line/payload cannot be parsed as a report message."""


def parse_line(line: str) -> dict:
    """Parse one line of a report topic payload (or replay capture line).

    Raises MalformedMessageError on anything that isn't a JSON object -
    callers should catch this, count it, and move on. Never let one bad
    line take down the subscription.
    """
    stripped = line.strip()
    if not stripped:
        raise MalformedMessageError("empty line")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise MalformedMessageError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MalformedMessageError(f"payload is not a JSON object: {type(payload).__name__}")
    return payload


@dataclasses.dataclass
class FieldRecord:
    """The last known value of one field, plus when we learned it."""

    raw: Any
    display: str
    msg_ts: Optional[float]  # timestamp field from the payload, if present
    wall_time: float  # local clock time.time() when this was processed
    note: str = ""


def format_age(now: float, wall_time: Optional[float]) -> str:
    if wall_time is None:
        return "never"
    delta = now - wall_time
    if delta < 0:
        delta = 0.0
    if delta < 1:
        return "just now"
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


class DashboardState:
    """Accumulates decoded field values across many delta messages.

    Nothing is ever cleared on a partial message - only individual keys
    present in that message's ``properties``/``packData``/top level are
    overwritten. Everything else keeps its previous FieldRecord untouched.
    """

    def __init__(self) -> None:
        self.hub: dict[str, FieldRecord] = {}
        self.undecoded: dict[str, FieldRecord] = {}
        self.packs: dict[str, dict[str, FieldRecord]] = {}
        self.pack_order: list[str] = []

        self.latest_msg_ts: Optional[float] = None
        self.messages_received: int = 0
        self.parse_errors: int = 0
        self.last_message_wall_time: Optional[float] = None
        self.connected: bool = False
        self.last_error: str = ""

    # -- ingestion ----------------------------------------------------

    def note_parse_error(self, reason: str = "") -> None:
        self.parse_errors += 1
        if reason:
            self.last_error = reason

    def apply_payload(self, payload: dict, wall_time: float) -> None:
        """Merge one already-parsed payload dict into the running state."""
        self.messages_received += 1
        self.last_message_wall_time = wall_time

        msg_ts = payload.get("timestamp")
        if isinstance(msg_ts, (int, float)):
            if self.latest_msg_ts is None or msg_ts > self.latest_msg_ts:
                self.latest_msg_ts = msg_ts
        else:
            msg_ts = None

        for key, value in payload.items():
            if key in decode.ENVELOPE_KEYS:
                continue
            spec = decode.TOP_LEVEL_INFO_FIELDS.get(key)
            if spec is not None:
                self._store(self.hub, spec, value, msg_ts, wall_time)
            else:
                self._store_undecoded(key, value, msg_ts, wall_time, source="top-level")

        properties = payload.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                spec = decode.HUB_FIELD_SPECS.get(key)
                if spec is not None:
                    self._store(self.hub, spec, value, msg_ts, wall_time)
                else:
                    self._store_undecoded(key, value, msg_ts, wall_time, source="properties")

        pack_data = payload.get("packData")
        if isinstance(pack_data, list):
            for entry in pack_data:
                if not isinstance(entry, dict):
                    continue
                sn = entry.get(decode.PACK_KEY_FIELD)
                if not sn:
                    # Can't group without a serial number - surface raw
                    # rather than silently drop it.
                    self._store_undecoded("packData[no-sn]", entry, msg_ts, wall_time, source="packData")
                    continue
                if sn not in self.packs:
                    self.packs[sn] = {}
                    self.pack_order.append(sn)
                pack_fields = self.packs[sn]
                for key, value in entry.items():
                    if key == decode.PACK_KEY_FIELD:
                        continue
                    spec = decode.PACK_FIELD_SPECS.get(key)
                    if spec is not None:
                        self._store(pack_fields, spec, value, msg_ts, wall_time)
                    else:
                        self._store_undecoded(
                            f"packData.{sn}.{key}", value, msg_ts, wall_time, source="packData"
                        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def _store(
        bucket: dict[str, FieldRecord],
        spec: "decode.FieldSpec",
        raw: Any,
        msg_ts: Optional[float],
        wall_time: float,
    ) -> None:
        bucket[spec.key] = FieldRecord(
            raw=raw,
            display=spec.format(raw),
            msg_ts=msg_ts,
            wall_time=wall_time,
            note=spec.note,
        )

    def _store_undecoded(
        self,
        key: str,
        raw: Any,
        msg_ts: Optional[float],
        wall_time: float,
        source: str,
    ) -> None:
        self.undecoded[key] = FieldRecord(
            raw=raw,
            display=decode.fmt_raw(raw),
            msg_ts=msg_ts,
            wall_time=wall_time,
            note=source,
        )
