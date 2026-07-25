"""Build the plain-text ASCII dashboard from a DashboardState snapshot.

Pure string building - no terminal control codes, no I/O. That keeps it
testable (assert on the returned text) and reusable from both the live
curses-free redraw loop and --replay's final snapshot print.
"""
from __future__ import annotations

from . import decode
from .state import DashboardState, FieldRecord, format_age

DEFAULT_WIDTH = 100
MIN_WIDTH = 24


def _truncate(line: str, width: int) -> str:
    if width <= 0 or len(line) <= width:
        return line
    if width <= 1:
        return line[:width]
    return line[: width - 1] + "…"


def _format_row(label: str, rec: FieldRecord | None, now: float, note: str = "") -> str:
    if rec is None:
        value = "--"
        age = "never seen"
    else:
        value = rec.display
        age = format_age(now, rec.wall_time)
        if rec.note and not note:
            note = rec.note
    suffix = f"  [{note}]" if note else ""
    return f"  {label:<24} {value:<16} ({age}){suffix}"


def _section_rows(state: DashboardState, section: str, now: float) -> list[str]:
    rows = []
    for key, spec in decode.HUB_FIELD_SPECS.items():
        if spec.section != section:
            continue
        rows.append(_format_row(spec.label, state.hub.get(key), now))
    if section == decode.SECTION_HUB_STATE:
        for key, spec in decode.TOP_LEVEL_INFO_FIELDS.items():
            rows.append(_format_row(spec.label, state.hub.get(key), now))
    return rows


def _pack_block(sn: str, fields: dict[str, FieldRecord], now: float) -> list[str]:
    lines = [f"  Pack {sn}"]
    for key, spec in decode.PACK_FIELD_SPECS.items():
        lines.append(_format_row("  " + spec.label, fields.get(key), now))

    max_rec = fields.get("maxVol")
    min_rec = fields.get("minVol")
    if max_rec is not None and min_rec is not None:
        imbalance_mv = (max_rec.raw - min_rec.raw) * 10
        older_wall = min(max_rec.wall_time, min_rec.wall_time)
        age = format_age(now, older_wall)
        value_str = f"{imbalance_mv} mV"
        lines.append(f"    {'Cell Imbalance':<24} {value_str:<16} ({age})")
    else:
        lines.append(f"    {'Cell Imbalance':<24} {'--':<16} (never seen)")
    return lines


def _undecoded_rows(state: DashboardState, now: float) -> list[str]:
    if not state.undecoded:
        return ["  (none observed yet)"]
    rows = []
    for key in sorted(state.undecoded):
        rec = state.undecoded[key]
        age = format_age(now, rec.wall_time)
        rows.append(f"  {key:<28} {rec.display:<16} ({age})  [source: {rec.note}]")
    return rows


def _connection_rows(state: DashboardState, now: float, mode: str) -> list[str]:
    if mode == "replay":
        conn = f"REPLAY FILE ({state.messages_received} lines processed)"
    else:
        conn = "CONNECTED" if state.connected else "DISCONNECTED"
    since_last = format_age(now, state.last_message_wall_time)
    rows = [
        f"  Mode:               {mode}",
        f"  Status:             {conn}",
        f"  Messages received:  {state.messages_received}",
        f"  Parse errors:       {state.parse_errors}",
        f"  Last message:       {since_last}",
    ]
    if state.last_error:
        rows.append(f"  Last parse error:   {state.last_error}")
    return rows


def render(state: DashboardState, *, width: int | None = None, now: float, mode: str = "live") -> str:
    """Render the full dashboard as plain text.

    ``now`` must be passed explicitly (rather than read from time.time()
    internally) so rendering stays a pure function callers can test with a
    fixed clock.
    """
    w = max(MIN_WIDTH, width or DEFAULT_WIDTH)

    out: list[str] = []
    out.append(_truncate("=" * w, w))
    out.append(_truncate(" Zendure SolarFlow MQTT Viewer (read-only)", w))
    out.append(_truncate("=" * w, w))

    sections = [
        ("Hub Live Power", decode.SECTION_POWER),
        ("Hub State", decode.SECTION_HUB_STATE),
        ("Hub Settings", decode.SECTION_HUB_SETTINGS),
    ]
    for title, section in sections:
        out.append("")
        out.append(_truncate(f"-- {title} --", w))
        for row in _section_rows(state, section, now):
            out.append(_truncate(row, w))

    out.append("")
    out.append(_truncate("-- Battery Packs --", w))
    if not state.pack_order:
        out.append("  (no pack data observed yet)")
    else:
        for sn in state.pack_order:
            for row in _pack_block(sn, state.packs[sn], now):
                out.append(_truncate(row, w))

    out.append("")
    out.append(_truncate("-- Undecoded / Unknown Fields --", w))
    for row in _undecoded_rows(state, now):
        out.append(_truncate(row, w))

    out.append("")
    out.append(_truncate("-- Connection Status --", w))
    for row in _connection_rows(state, now, mode):
        out.append(_truncate(row, w))

    out.append("")
    return "\n".join(out)
