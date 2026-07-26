"""Pure dashboard layout: builds a fixed-size grid of styled text ("Frame")
from a DashboardState snapshot. No curses, no I/O - this is what makes the
layout testable headlessly (render into a WxH buffer, assert on it) and
what lets --replay/--once print the exact same thing curses would draw.

The dashboard is tabbed (Overview / Hub / Packs / Raw). Only the active
tab's content is built, which is what makes fitting everything on one
screen possible - see build_frame().
"""
from __future__ import annotations

import dataclasses
import re
from typing import Optional

from . import decode
from .state import DashboardState, FieldRecord

TABS = ["Overview", "Hub", "Packs", "Raw"]

MIN_COLS = 54
MIN_ROWS = 14

STALE_AFTER_SECONDS = 30.0

_ENUM_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


# ---------------------------------------------------------------------------
# Frame primitives
# ---------------------------------------------------------------------------


# Span.attr is a space-separated token set: at most one colour name from
# COLORS, plus any emphasis flags. tui.py resolves it to curses attributes;
# plain-text output ignores attrs entirely, so colour never changes a single
# character of what --once prints or what the layout tests assert on.
COLORS = ("accent", "ok", "warn", "error", "info")
EMPHASIS = ("normal", "bold", "dim", "reverse", "muted")


@dataclasses.dataclass(frozen=True)
class Span:
    text: str
    attr: str = "normal"


@dataclasses.dataclass
class Frame:
    cols: int
    rows: int
    lines: list[list[Span]]

    def to_text(self) -> str:
        return "\n".join("".join(sp.text for sp in line) for line in self.lines)

    def line_widths_ok(self) -> bool:
        return all(sum(len(sp.text) for sp in line) <= self.cols for line in self.lines)


def fit(s: str, width: int) -> str:
    """Pad or truncate ``s`` to exactly ``width`` characters."""
    if width <= 0:
        return ""
    if len(s) > width:
        if width == 1:
            return s[:1]
        return s[: width - 1] + "…"
    return s + " " * (width - len(s))


def _row(spans: list[Span], cols: int) -> list[Span]:
    """Normalize a list of spans to exactly ``cols`` total characters."""
    total = sum(len(sp.text) for sp in spans)
    if total == cols:
        return spans
    if total < cols:
        return spans + [Span(" " * (cols - total))]
    # over budget: truncate from the end, span by span
    out: list[Span] = []
    remaining = cols
    for sp in spans:
        if remaining <= 0:
            break
        if len(sp.text) <= remaining:
            out.append(sp)
            remaining -= len(sp.text)
        else:
            out.append(Span(fit(sp.text, remaining), sp.attr))
            remaining = 0
    return out


def blank_frame(cols: int, rows: int) -> Frame:
    return Frame(cols, rows, [[Span(" " * cols)] for _ in range(rows)])


# ---------------------------------------------------------------------------
# Small display helpers
# ---------------------------------------------------------------------------


def short_age(now: float, wall_time: Optional[float]) -> str:
    if wall_time is None:
        return "--"
    d = max(0.0, now - wall_time)
    if d < 1:
        return "now"
    if d < 60:
        return f"{d:.0f}s"
    if d < 3600:
        return f"{d / 60:.0f}m"
    if d < 86400:
        return f"{d / 3600:.0f}h"
    return f"{d / 86400:.0f}d"


def is_stale(now: float, wall_time: Optional[float], threshold: float = STALE_AFTER_SECONDS) -> bool:
    if wall_time is None:
        return False  # "never seen" isn't "stale", it's a different marker
    return (now - wall_time) > threshold


def strip_enum_suffix(text: str) -> str:
    """"Discharging (2)" -> "Discharging" - saves width in compact views."""
    return _ENUM_SUFFIX_RE.sub("", text)


def gauge(percent: Optional[float], width: int = 14) -> str:
    if percent is None:
        return "[" + "-" * width + "] --"
    pct = max(0.0, min(100.0, percent))
    filled = round(pct / 100.0 * width)
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:.0f}%"


def soc_color(percent: Optional[float]) -> str:
    """Colour for a state-of-charge reading: red near empty, green when full."""
    if percent is None:
        return "muted"
    if percent < 20:
        return "error"
    if percent < 50:
        return "warn"
    return "ok"


def gauge_spans(percent: Optional[float], width: int = 14) -> list[Span]:
    """gauge() split into coloured spans - identical text, per-part colour."""
    if percent is None:
        return [Span(gauge(None, width), "muted")]
    pct = max(0.0, min(100.0, percent))
    filled = max(0, min(width, round(pct / 100.0 * width)))
    color = soc_color(pct)
    return [
        Span("[", "muted"),
        Span("█" * filled, color),
        Span("░" * (width - filled), "muted"),
        Span("]", "muted"),
        Span(f" {pct:.0f}%", f"bold {color}"),
    ]


def _value_and_age(rec: Optional[FieldRecord], now: float, strip_enum: bool = False) -> tuple[str, str, bool]:
    if rec is None:
        return "--", "--", False
    text = strip_enum_suffix(rec.display) if strip_enum else rec.display
    return text, short_age(now, rec.wall_time), is_stale(now, rec.wall_time)


# ---------------------------------------------------------------------------
# Border / shell builders
# ---------------------------------------------------------------------------


def _top_border(cols: int, title: str, right: str, right_attr: str = "bold") -> list[Span]:
    inner = cols - 2
    left = f" {title} "
    right_s = f" {right} " if right else " "
    if len(left) + len(right_s) > inner:
        keep = max(0, inner - len(right_s) - 2)
        left = f" {title[:keep]}" + (" " if keep >= len(title) else "…")
    dashes = max(0, inner - len(left) - len(right_s))
    content = fit(left + "─" * dashes + right_s, inner)
    # Slice the already-fitted string rather than re-assembling it, so
    # colouring the pieces cannot change the total width.
    cut1 = min(len(left), len(content))
    cut2 = min(len(left) + dashes, len(content))
    return [
        Span("┌", "muted"),
        Span(content[:cut1], "bold accent"),
        Span(content[cut1:cut2], "muted"),
        Span(content[cut2:], right_attr),
        Span("┐", "muted"),
    ]


def _bottom_border(cols: int) -> list[Span]:
    return [Span("└", "muted"), Span("─" * (cols - 2), "muted"), Span("┘", "muted")]


def _divider(cols: int, split_at: Optional[int] = None) -> list[Span]:
    inner = cols - 2
    if split_at is None:
        return [Span("├", "muted"), Span("─" * inner, "muted"), Span("┤", "muted")]
    left_w = max(0, split_at)
    right_w = max(0, inner - left_w - 1)
    return [
        Span("├", "muted"),
        Span("─" * left_w, "muted"),
        Span("┬", "muted"),
        Span("─" * right_w, "muted"),
        Span("┤", "muted"),
    ]


def _tab_bar(cols: int, active_index: int) -> list[Span]:
    inner = cols - 2
    spans: list[Span] = [Span(" ")]
    for i, name in enumerate(TABS):
        active = i == active_index
        label = f"[{i + 1}] {name}" if active else f" {i + 1} {name} "
        # reverse + a colour pair paints the active tab as a coloured block.
        spans.append(Span(label, "reverse accent bold" if active else "muted"))
        spans.append(Span("  "))
    return [Span("│", "muted")] + _row(spans, inner) + [Span("│", "muted")]


def _status_bar(cols: int, state: DashboardState, now: float, mode: str) -> list[Span]:
    since = short_age(now, state.last_message_wall_time)
    inner = cols - 2

    # No error text is ever rendered here, or anywhere else in the frame.
    # Broker reason strings are arbitrary length and arbitrary content; they
    # used to take over this row, and any error text at all - however well
    # fitted - reads as damage on a dashboard whose whole point is a stable
    # one-screen layout. Errors go to the log file (see errorlog.py); the
    # screen carries the *state* they produced, which is the DISCONNECTED
    # marker in the title bar and the error count below.
    if mode == "replay":
        left = f" replay · {state.messages_received} lines · errors {state.parse_errors} "
    else:
        left = f" msgs {state.messages_received} · errors {state.parse_errors} · last {since} "
    hint = " [1-4/Tab] switch  [q] quit "
    gap = max(1, inner - len(left) - len(hint))
    content = fit(left + " " * gap + hint, inner)
    cut = min(len(left), len(content))
    return [
        Span("│", "muted"),
        Span(content[:cut], "normal"),
        Span(content[cut:], "muted"),
        Span("│", "muted"),
    ]


def _connection_label(state: DashboardState, now: float, mode: str) -> str:
    if mode == "replay":
        return f"REPLAY · {short_age(now, state.last_message_wall_time)}"
    status = "CONNECTED" if state.connected else "DISCONNECTED"
    return f"{status} · {short_age(now, state.last_message_wall_time)}"


def _connection_attr(state: DashboardState, mode: str) -> str:
    # Colour on the fixed word DISCONNECTED, never on a reason string: this
    # is link state, not an error message, and it occupies the same cells
    # whatever went wrong.
    if mode == "replay":
        return "bold info"
    if not state.connected:
        return "bold error"
    return "bold ok"


# ---------------------------------------------------------------------------
# Multi-column content row helpers
# ---------------------------------------------------------------------------


def _split_divider(cols: int, widths: list[int]) -> list[Span]:
    spans: list[Span] = [Span("├", "muted")]
    for i, w in enumerate(widths):
        spans.append(Span("─" * w, "muted"))
        spans.append(Span("┬" if i < len(widths) - 1 else "┤", "muted"))
    return _row(spans, cols)


def _bottom_split_divider(cols: int, widths: list[int]) -> list[Span]:
    spans: list[Span] = [Span("├", "muted")]
    for i, w in enumerate(widths):
        spans.append(Span("─" * w, "muted"))
        spans.append(Span("┴" if i < len(widths) - 1 else "┤", "muted"))
    return _row(spans, cols)


def _kv_lines(width: int, rows: list[tuple[str, str, str, bool]]) -> list[list[Span]]:
    """rows: (label, value, age, stale) -> one Span-line per row, width-fit.

    Layout: label | gap | value | gap | age, three fixed-width fields plus
    two single-space gaps, summing to exactly ``width``.
    """
    out = []
    label_w = max(8, min(16, width // 2))
    age_w = 4
    value_w = max(1, width - label_w - age_w - 2)
    for label, value, age, stale in rows:
        spans = [
            Span(fit(label, label_w), "muted"),
            Span(" "),
            Span(fit(value, value_w), "dim" if stale else "normal"),
            Span(" "),
            Span(fit(age, age_w), "dim"),
        ]
        out.append(_row(spans, width))
    return out


# ---------------------------------------------------------------------------
# Tab bodies (each returns a list of Span-lines, exactly `height` of them,
# each exactly `width` wide)
# ---------------------------------------------------------------------------


def _overview_body(state: DashboardState, width: int, height: int, now: float):
    # ``width`` is the inner content width; reserve 1 column for the
    # vertical divider drawn between the two panels.
    content_w = max(2, width - 1)
    left_w = content_w // 2
    right_w = content_w - left_w

    hub = state.hub
    soc_rec = hub.get("electricLevel")
    soc_val = soc_rec.raw if soc_rec is not None else None

    pack_rec = None
    if state.pack_order:
        pack_rec = state.packs[state.pack_order[0]]
    soh_text, soh_age, soh_stale = _value_and_age(pack_rec.get("soh") if pack_rec else None, now)
    temp_text, temp_age, temp_stale = _value_and_age(pack_rec.get("maxTemp") if pack_rec else None, now)

    pack_state_raw = hub.get("packState").raw if hub.get("packState") is not None else None
    # Hub-perspective naming: outputPackPower is the hub feeding the pack
    # (charging), packInputPower is the pack feeding the hub (discharging).
    charge_p = hub.get("outputPackPower")
    discharge_p = hub.get("packInputPower")

    if pack_state_raw == 1 or (pack_state_raw is None and charge_p and charge_p.raw):
        batt_label = "Charging"
        batt_icon = "▲"
        batt_power = charge_p.raw if charge_p else None
        connector = "▼"
        batt_color = "ok"
    elif pack_state_raw == 2 or (pack_state_raw is None and discharge_p and discharge_p.raw):
        batt_label = "Discharging"
        batt_icon = "▼"
        batt_power = discharge_p.raw if discharge_p else None
        connector = "▲"
        batt_color = "warn"
    else:
        batt_label = "Idle"
        batt_icon = "·"
        batt_power = 0
        connector = "·"
        batt_color = "muted"

    floor_rec = hub.get("minSoc")
    target_rec = hub.get("socSet")
    floor_txt = f"{floor_rec.raw / 10:.0f}%" if floor_rec is not None else "--"
    target_txt = f"{target_rec.raw / 10:.0f}%" if target_rec is not None else "--"

    batt_w = f"{batt_power if batt_power is not None else '--'} W"
    left_lines_raw: list[list[Span]] = [
        [Span("BATTERY", "bold accent")],
        gauge_spans(soc_val, min(20, left_w - 8)),
        [Span(f"SoH {soh_text}   {temp_text}", "dim" if (soh_stale or temp_stale) else "normal")],
        [Span("")],
        [Span(f"{batt_label}  {batt_icon} {batt_w}", f"bold {batt_color}")],
        [Span("")],
        [Span("Floor ", "muted"), Span(floor_txt), Span("   Target ", "muted"), Span(target_txt)],
    ]

    solar_rec = hub.get("solarInputPower")
    home_rec = hub.get("outputHomePower")
    solar_txt = f"{solar_rec.raw} W" if solar_rec is not None else "-- W"
    home_txt = f"{home_rec.raw} W" if home_rec is not None else "-- W"
    batt_txt = f"{batt_power} W" if batt_power is not None else "-- W"
    solar_color = "warn" if (solar_rec is not None and solar_rec.raw) else "muted"

    right_lines_raw: list[list[Span]] = [
        [Span("POWER FLOW", "bold accent")],
        [Span(" Solar  ", "bold"), Span(solar_txt, f"bold {solar_color}")],
        [Span("    │", "muted")],
        [Span("    ▼", solar_color)],
        [Span(" [ HUB ] ", "bold accent"), Span("──▶", "muted"), Span(" Home  ", "bold"), Span(home_txt, "bold info")],
        [Span(f"    {connector}", batt_color)],
        [Span(" Battery ", "normal"), Span(batt_icon, batt_color), Span(f" {batt_txt}", batt_color)],
    ]

    def pad_to(lines: list[list[Span]], w: int, h: int) -> list[list[Span]]:
        out = [_row([Span("  ")] + line, w) for line in lines]
        while len(out) < h:
            out.append(_row([], w))
        return out[:h]

    left_col = pad_to(left_lines_raw, left_w, height)
    right_col = pad_to(right_lines_raw, right_w, height)

    return left_col, right_col, [left_w, right_w]


def _hub_body(state: DashboardState, width: int, height: int, now: float):
    # ``width`` is the inner content width; reserve 1 column for the
    # vertical divider drawn between the two panels.
    content_w = max(2, width - 1)
    left_w = content_w // 2
    right_w = content_w - left_w

    state_keys = [
        ("electricLevel", "SoC"),
        ("packState", "Pack"),
        ("pass", "Bypass"),
        ("hubState", "Hub"),
        ("wifiState", "WiFi"),
        ("heatState", "Heater"),
        ("packNum", "Pack Count"),
        ("remainOutTime", "Discharge"),
        ("remainInputTime", "Charge"),
        ("wifiName", "SSID"),
        ("mac", "MAC"),
        ("ip", "IP"),
    ]
    settings_keys = [
        ("outputLimit", "Output Limit"),
        ("inputLimit", "Input Limit"),
        ("inverseMaxPower", "Inv Max Pwr"),
        ("minSoc", "Min SoC"),
        ("socSet", "Target SoC"),
        ("passMode", "Bypass Mode"),
        ("autoModel", "Auto Model"),
        ("smartMode", "Smart Mode"),
        ("masterSwitch", "Master Sw"),
        ("buzzerSwitch", "Buzzer"),
        ("autoRecover", "Auto Recover"),
        ("inputMode", "Input Mode"),
        ("pvBrand", "PV Brand"),
        ("blueOta", "BT OTA"),
        ("masterSoftVersion", "Soft Ver"),
        ("masterhaerVersion", "Hard Ver"),
    ]

    def rows_for(keys):
        out = []
        for key, label in keys:
            text, age, stale = _value_and_age(state.hub.get(key), now, strip_enum=True)
            out.append((label, text, age, stale))
        return out

    left_header = [Span(fit("STATE", left_w), "bold accent")]
    right_header = [Span(fit("SETTINGS", right_w), "bold accent")]

    left_kv = _kv_lines(left_w, rows_for(state_keys))
    right_kv = _kv_lines(right_w, rows_for(settings_keys))

    def build_col(header, kv, w):
        lines = [header] + kv
        avail = height
        if len(lines) > avail:
            shown = lines[: max(0, avail - 1)]
            hidden = len(lines) - len(shown)
            shown.append([Span(fit(f"+{hidden} more", w), "dim")])
            lines = shown
        while len(lines) < avail:
            lines.append([Span(" " * w)])
        return [_row(line, w) for line in lines]

    left_col = build_col(left_header, left_kv, left_w)
    right_col = build_col(right_header, right_kv, right_w)

    return left_col, right_col, [left_w, right_w]


PACK_COLUMNS = [
    ("SN", 16),
    ("SoC", 5),
    ("SoH", 5),
    ("Temp", 6),
    ("Vmax", 6),
    ("Vmin", 6),
    ("dV(mV)", 7),
    ("Power", 7),
    ("State", 12),
    ("Age", 4),
]


def _packs_body(state: DashboardState, width: int, height: int, now: float) -> list[list[Span]]:
    if not state.pack_order:
        lines = [_row([Span("(no pack data observed yet)")], width)]
        while len(lines) < height:
            lines.append(_row([], width))
        return lines[:height]

    col_defs = list(PACK_COLUMNS)
    total_fixed = sum(w for _, w in col_defs) + (len(col_defs) - 1)
    if total_fixed < width:
        # give extra room to the two free-text columns (SN, State) rather
        # than dumping it all into one very wide column
        extra = width - total_fixed
        sn_add = (extra * 2) // 3
        state_add = extra - sn_add
        col_defs[0] = (col_defs[0][0], col_defs[0][1] + sn_add)
        col_defs[8] = (col_defs[8][0], col_defs[8][1] + state_add)

    def header_row():
        spans = []
        for i, (label, w) in enumerate(col_defs):
            spans.append(Span(fit(label, w), "bold accent"))
            if i < len(col_defs) - 1:
                spans.append(Span(" "))
        return _row(spans, width)

    def data_row(sn: str):
        fields = state.packs[sn]
        soc, _, soc_stale = _value_and_age(fields.get("socLevel"), now)
        soh, _, soh_stale = _value_and_age(fields.get("soh"), now)
        temp, _, temp_stale = _value_and_age(fields.get("maxTemp"), now)
        vmax_rec = fields.get("maxVol")
        vmin_rec = fields.get("minVol")
        vmax, _, vmax_stale = _value_and_age(vmax_rec, now)
        vmin, _, vmin_stale = _value_and_age(vmin_rec, now)
        power, _, power_stale = _value_and_age(fields.get("power"), now)
        state_txt, _, state_stale = _value_and_age(fields.get("state"), now, strip_enum=True)

        if vmax_rec is not None and vmin_rec is not None:
            dv = f"{(vmax_rec.raw - vmin_rec.raw) * 10}"
        else:
            dv = "--"

        # overall age = most recent update across the pack's fields
        wall_times = [r.wall_time for r in fields.values() if r.wall_time is not None]
        age = short_age(now, max(wall_times)) if wall_times else "--"

        values = [
            (sn, False),
            (soc.replace(" %", ""), soc_stale),
            (soh.replace(" %", ""), soh_stale),
            (temp.replace(" °C", ""), temp_stale),
            (vmax.replace(" V", ""), vmax_stale),
            (vmin.replace(" V", ""), vmin_stale),
            (dv, False),
            (power.replace(" W", ""), power_stale),
            (state_txt, state_stale),
            (age, False),
        ]
        spans = []
        for (text, stale), (_, w) in zip(values, col_defs):
            spans.append(Span(fit(text, w), "dim" if stale else "normal"))
            spans.append(Span(" "))
        return _row(spans, width)

    lines = [header_row(), _row([Span("─" * width, "muted")], width)]
    for sn in state.pack_order:
        lines.append(data_row(sn))

    while len(lines) < height:
        lines.append(_row([], width))
    if len(lines) > height:
        shown = lines[: height - 1]
        hidden = len(lines) - len(shown)
        shown.append(_row([Span(fit(f"+{hidden} more rows not shown", width), "dim")], width))
        lines = shown
    return lines[:height]


def _raw_body(state: DashboardState, width: int, height: int, now: float) -> list[list[Span]]:
    col_widths = [max(10, width - 10 - 12 - 6 - 3), 12, 10, 6]

    def header_row():
        labels = ["Field", "Value", "Source", "Age"]
        spans = []
        for label, w in zip(labels, col_widths):
            spans.append(Span(fit(label, w), "bold accent"))
            spans.append(Span(" "))
        return _row(spans, width)

    lines = [header_row(), _row([Span("─" * width, "muted")], width)]
    if not state.undecoded:
        lines.append(_row([Span("(none observed yet)", "dim")], width))
    else:
        for key in sorted(state.undecoded):
            rec = state.undecoded[key]
            age = short_age(now, rec.wall_time)
            stale = is_stale(now, rec.wall_time)
            values = [key, decode.fmt_raw(rec.raw), rec.note, age]
            spans = []
            for text, w in zip(values, col_widths):
                spans.append(Span(fit(text, w), "dim" if stale else "normal"))
                spans.append(Span(" "))
            lines.append(_row(spans, width))

    while len(lines) < height:
        lines.append(_row([], width))
    if len(lines) > height:
        shown = lines[: height - 1]
        hidden = len(lines) - len(shown)
        shown.append(_row([Span(fit(f"+{hidden} more not shown", width), "dim")], width))
        lines = shown
    return lines[:height]


# ---------------------------------------------------------------------------
# Fallback for terminals too small to lay out
# ---------------------------------------------------------------------------


def _fallback_frame(cols: int, rows: int) -> Frame:
    msg = f"Terminal too small ({cols}x{rows}). Need at least {MIN_COLS}x{MIN_ROWS}."
    lines = []
    mid = rows // 2
    for r in range(rows):
        if r == mid:
            text = fit(msg, cols) if cols >= len(msg) else fit(msg[: max(0, cols)], cols)
            lines.append([Span(text, "bold warn")])
        else:
            lines.append([Span(" " * cols)])
    return Frame(cols, rows, lines)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def build_frame(
    state: DashboardState,
    tab: str,
    cols: int,
    rows: int,
    now: float,
    mode: str = "live",
    device_id: str = "",
) -> Frame:
    """Build the full one-screen dashboard Frame for the given tab.

    ``tab`` is one of TABS (case-insensitive). Always returns a Frame with
    exactly ``rows`` lines, each exactly ``cols`` characters - callers never
    need to clip further.
    """
    cols = max(1, cols)
    rows = max(1, rows)
    if cols < MIN_COLS or rows < MIN_ROWS:
        return _fallback_frame(cols, rows)

    tab_names_lower = [t.lower() for t in TABS]
    try:
        active_index = tab_names_lower.index(tab.lower())
    except ValueError:
        active_index = 0

    title = f"Zendure SolarFlow · {device_id}" if device_id else "Zendure SolarFlow"
    right = _connection_label(state, now, mode)

    lines: list[list[Span]] = []
    lines.append(_top_border(cols, title, right, _connection_attr(state, mode)))
    lines.append(_tab_bar(cols, active_index))

    # 2 fixed header rows + top divider + bottom divider + status + bottom border = 6 non-body rows
    body_height = max(1, rows - 6)
    inner = cols - 2

    if active_index == 0:
        left_col, right_col, widths = _overview_body(state, inner, body_height, now)
        lines.append(_split_divider(cols, widths))
        for l_line, r_line in zip(left_col, right_col):
            lines.append(_row([Span("│", "muted")] + l_line + [Span("│", "muted")] + r_line + [Span("│", "muted")], cols))
        lines.append(_bottom_split_divider(cols, widths))
    elif active_index == 1:
        left_col, right_col, widths = _hub_body(state, inner, body_height, now)
        lines.append(_split_divider(cols, widths))
        for l_line, r_line in zip(left_col, right_col):
            lines.append(_row([Span("│", "muted")] + l_line + [Span("│", "muted")] + r_line + [Span("│", "muted")], cols))
        lines.append(_bottom_split_divider(cols, widths))
    elif active_index == 2:
        body_lines = _packs_body(state, inner, body_height, now)
        lines.append(_divider(cols))
        for line in body_lines:
            lines.append(_row([Span("│", "muted")] + line + [Span("│", "muted")], cols))
        lines.append(_divider(cols))
    else:
        body_lines = _raw_body(state, inner, body_height, now)
        lines.append(_divider(cols))
        for line in body_lines:
            lines.append(_row([Span("│", "muted")] + line + [Span("│", "muted")], cols))
        lines.append(_divider(cols))

    lines.append(_status_bar(cols, state, now, mode))
    lines.append(_bottom_border(cols))

    # Safety net: force exact row/col count no matter what the section
    # builders produced.
    fixed_lines = [_row(line, cols) for line in lines[:rows]]
    while len(fixed_lines) < rows:
        fixed_lines.append(_row([], cols))

    return Frame(cols, rows, fixed_lines)
