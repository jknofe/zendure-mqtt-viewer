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
import logging
from typing import Any, Optional

from . import decode

# Errors are recorded, never rendered: the dashboard shows counts and link
# state, the detail goes to the log file. See errorlog.py. Until that module
# configures a handler this logger discards everything (NullHandler in
# __init__), so importing state.py still costs no I/O.
logger = logging.getLogger(__name__)


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
        # last_error / connection_error are a record for the log and for
        # tests, not something the dashboard draws - no error string is ever
        # rendered into a frame.
        self.last_error: str = ""
        # Link-level problem (refused connect, unexpected disconnect). Kept
        # separate from last_error, which is about message *content*: a bad
        # payload says nothing about the connection and must not clear it.
        self.connection_error: str = ""
        # When a full-report request was last sent (--allow-refresh only).
        self.last_refresh_request: Optional[float] = None

    # -- ingestion ----------------------------------------------------

    def note_parse_error(self, reason: str = "") -> None:
        self.parse_errors += 1
        if reason:
            self.last_error = reason
            # Logged here rather than in each caller so a malformed line is
            # recorded identically whether it arrived over MQTT or out of a
            # replay file.
            logger.warning("parse error: %s", reason)

    # -- connection status --------------------------------------------

    def note_connected(self) -> None:
        self.connected = True
        self.connection_error = ""

    def note_connection_error(self, reason: str) -> None:
        """Record why the link is down. Never sets connected=True."""
        self.connected = False
        self.connection_error = reason or "connection failed"

    def note_refresh_requested(self, now: float) -> None:
        """A full-report request just went out; the status bar acknowledges it."""
        self.last_refresh_request = now

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

    # -- snapshot / restore ---------------------------------------------
    #
    # The stream is delta-only: rare fields (packState, minSoc, socSet,
    # packNum, firmware versions) are re-broadcast infrequently, so a fresh
    # process can sit for a long time with "--" where those values belong.
    # Carrying the last known values across restarts removes that blind
    # window. Only the *raw* values are persisted, never the formatted text -
    # display strings are re-derived through the same FieldSpec used live, so
    # a decode fix applies to restored values too instead of resurrecting the
    # old wording from the cache file.

    def to_snapshot(self) -> dict:
        """A JSON-serializable dump of every last-seen value."""

        def dump(bucket: dict[str, FieldRecord]) -> dict:
            return {
                key: {
                    "raw": rec.raw,
                    "msg_ts": rec.msg_ts,
                    "wall_time": rec.wall_time,
                    "note": rec.note,
                }
                for key, rec in bucket.items()
            }

        return {
            "latest_msg_ts": self.latest_msg_ts,
            "last_message_wall_time": self.last_message_wall_time,
            "hub": dump(self.hub),
            "undecoded": dump(self.undecoded),
            "packs": {sn: dump(fields) for sn, fields in self.packs.items()},
            "pack_order": list(self.pack_order),
        }

    def restore_snapshot(self, data: dict) -> int:
        """Merge a snapshot back in. Returns the number of fields restored.

        Restored records keep their original wall_time, so the age column and
        the staleness dimming tell the truth: these are old readings, not
        something that just arrived. Live messages overwrite them key by key
        as they come in. Anything malformed is skipped rather than raising -
        a damaged cache file must never stop the dashboard from starting.
        """
        if not isinstance(data, dict):
            return 0
        restored = 0

        def load(bucket: dict[str, FieldRecord], entries, resolve_spec) -> int:
            n = 0
            if not isinstance(entries, dict):
                return 0
            for key, entry in entries.items():
                if not isinstance(entry, dict) or "raw" not in entry:
                    continue
                wall_time = entry.get("wall_time")
                if not isinstance(wall_time, (int, float)):
                    continue
                msg_ts = entry.get("msg_ts")
                if not isinstance(msg_ts, (int, float)):
                    msg_ts = None
                raw = entry["raw"]
                spec = resolve_spec(key)
                if spec is not None:
                    self._store(bucket, spec, raw, msg_ts, float(wall_time))
                else:
                    note = entry.get("note")
                    bucket[key] = FieldRecord(
                        raw=raw,
                        display=decode.fmt_raw(raw),
                        msg_ts=msg_ts,
                        wall_time=float(wall_time),
                        note=note if isinstance(note, str) else "",
                    )
                n += 1
            return n

        restored += load(self.hub, data.get("hub"), _resolve_hub_spec)
        restored += load(self.undecoded, data.get("undecoded"), lambda key: None)

        packs = data.get("packs")
        if isinstance(packs, dict):
            for sn, fields in packs.items():
                if sn not in self.packs:
                    self.packs[sn] = {}
                    self.pack_order.append(sn)
                restored += load(self.packs[sn], fields, decode.PACK_FIELD_SPECS.get)

        # Keep pack_order deterministic across restarts.
        for sn in data.get("pack_order") or []:
            if sn in self.packs and sn in self.pack_order:
                self.pack_order.remove(sn)
                self.pack_order.append(sn)

        latest = data.get("latest_msg_ts")
        if isinstance(latest, (int, float)) and (
            self.latest_msg_ts is None or latest > self.latest_msg_ts
        ):
            self.latest_msg_ts = latest

        # Deliberately *not* restored: last_message_wall_time (nothing has
        # arrived in this run yet, and the header's "last message" age must
        # not claim otherwise), messages_received, and parse_errors.
        return restored


def _resolve_hub_spec(key: str):
    return decode.HUB_FIELD_SPECS.get(key) or decode.TOP_LEVEL_INFO_FIELDS.get(key)
