"""Tests for the pure dashboard layout (zendure_mqtt_viewer.layout).

Headless - builds Frames into fixed WxH buffers and asserts on them, the
way --replay / --once do, without ever touching curses or a terminal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zendure_mqtt_viewer import layout
from zendure_mqtt_viewer.replay import replay_file
from zendure_mqtt_viewer.state import DashboardState

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _populated_state() -> DashboardState:
    state = DashboardState()
    replay_file(SAMPLES_DIR / "sample_capture_1.jsonl", state)
    return state


# ---------------------------------------------------------------------------
# One-screen property: the core complaint being fixed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cols,rows", [(80, 24), (120, 30), (100, 27), (54, 14)])
@pytest.mark.parametrize("tab", layout.TABS)
def test_frame_never_exceeds_requested_size(cols, rows, tab):
    state = _populated_state()
    frame = layout.build_frame(state, tab, cols, rows, now=1784980800.0, mode="replay")
    assert len(frame.lines) <= rows
    for line in frame.lines:
        width = sum(len(sp.text) for sp in line)
        assert width <= cols, f"{tab} line exceeds {cols} cols: {width}"


@pytest.mark.parametrize("cols,rows", [(80, 24), (120, 30)])
def test_frame_is_exactly_the_requested_size_when_large_enough(cols, rows):
    # Not just "at most" - the dashboard should fill the screen cleanly,
    # not leave a ragged partial frame.
    state = _populated_state()
    frame = layout.build_frame(state, "overview", cols, rows, now=1784980800.0, mode="replay")
    assert len(frame.lines) == rows
    for line in frame.lines:
        assert sum(len(sp.text) for sp in line) == cols


def test_empty_state_also_fits_one_screen():
    state = DashboardState()
    for tab in layout.TABS:
        frame = layout.build_frame(state, tab, 80, 24, now=1.0, mode="live")
        assert len(frame.lines) <= 24
        assert all(sum(len(sp.text) for sp in line) <= 80 for line in frame.lines)


# ---------------------------------------------------------------------------
# Tabs actually change content, and the active tab is visibly marked
# ---------------------------------------------------------------------------


def test_switching_tabs_changes_body_content():
    state = _populated_state()
    texts = {
        tab: layout.build_frame(state, tab, 100, 27, now=1784980800.0, mode="replay").to_text()
        for tab in layout.TABS
    }
    assert texts["Overview"] != texts["Hub"]
    assert texts["Hub"] != texts["Packs"]
    assert texts["Packs"] != texts["Raw"]
    # tab-specific markers
    assert "BATTERY" in texts["Overview"] and "BATTERY" not in texts["Packs"]
    assert "ZZ0EXAMPLE00001" in texts["Packs"]
    assert "outputHomePowerCycle" in texts["Raw"]


def test_active_tab_label_is_bracketed_and_reverse_video():
    state = _populated_state()
    frame = layout.build_frame(state, "hub", 100, 27, now=1784980800.0, mode="replay")
    tab_bar_text = frame.to_text().splitlines()[1]
    assert "[2] Hub" in tab_bar_text

    tab_bar_line = frame.lines[1]
    reverse_spans = [sp for sp in tab_bar_line if "reverse" in sp.attr.split()]
    assert reverse_spans, "expected at least one reverse-video span on the tab bar"
    assert any("Hub" in sp.text for sp in reverse_spans)


def test_unknown_tab_name_falls_back_to_first_tab():
    state = _populated_state()
    frame = layout.build_frame(state, "not-a-real-tab", 100, 27, now=1784980800.0, mode="replay")
    assert "BATTERY" in frame.to_text()  # overview content


# ---------------------------------------------------------------------------
# Status bar always present, on every tab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tab", layout.TABS)
def test_status_bar_present_on_every_tab(tab):
    state = _populated_state()
    frame = layout.build_frame(state, tab, 100, 27, now=1784980800.0, mode="replay")
    text = frame.to_text()
    assert "msgs" in text or "replay" in text
    assert "errors" in text
    assert "quit" in text


def test_status_bar_reflects_message_and_error_counts():
    state = _populated_state()
    frame = layout.build_frame(state, "overview", 100, 27, now=1784980800.0, mode="replay")
    text = frame.to_text()
    assert str(state.messages_received) in text
    assert str(state.parse_errors) in text


# ---------------------------------------------------------------------------
# Small-terminal fallback
# ---------------------------------------------------------------------------


def test_tiny_terminal_uses_fallback_not_a_crash():
    state = _populated_state()
    frame = layout.build_frame(state, "overview", 20, 6, now=1784980800.0, mode="replay")
    assert len(frame.lines) == 6
    assert all(len("".join(sp.text for sp in line)) == 20 for line in frame.lines)


def test_degenerate_1x1_terminal_does_not_crash():
    state = _populated_state()
    frame = layout.build_frame(state, "overview", 1, 1, now=1784980800.0, mode="replay")
    assert len(frame.lines) == 1
    assert len(frame.lines[0][0].text) == 1


# ---------------------------------------------------------------------------
# Charge/discharge direction
#
# Zendure names the two pack power fields from the hub's point of view:
# outputPackPower is the hub pushing into the pack (charging) and
# packInputPower is the pack feeding the hub (discharging). The samples
# confirm it: packState=1 always comes with outputPackPower > 0, and
# packState=2 with packInputPower > 0.
# ---------------------------------------------------------------------------


def _overview_text(state: DashboardState) -> str:
    frame = layout.build_frame(state, "overview", 100, 27, now=1784980800.0, mode="replay")
    return "\n".join("".join(sp.text for sp in line) for line in frame.lines)


def _state_with(props: dict) -> DashboardState:
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": props}, wall_time=1784980800.0)
    return state


def test_charging_is_reported_with_the_hub_to_pack_power():
    text = _overview_text(_state_with({"packState": 1, "outputPackPower": 20, "packInputPower": 0}))
    assert "Charging" in text
    assert "Discharging" not in text
    assert "20 W" in text


def test_discharging_is_reported_with_the_pack_to_hub_power():
    text = _overview_text(_state_with({"packState": 2, "packInputPower": 355, "outputPackPower": 0}))
    assert "Discharging" in text
    assert "355 W" in text


@pytest.mark.parametrize(
    "props,expected",
    [
        ({"outputPackPower": 20}, "Charging"),
        ({"packInputPower": 355}, "Discharging"),
    ],
)
def test_direction_without_packstate_falls_back_to_the_right_field(props, expected):
    # packState is not in every delta, so the power fields alone must give
    # the same answer they would with it.
    text = _overview_text(_state_with(props))
    assert expected in text
    if expected == "Charging":
        assert "Discharging" not in text


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_gauge_full_and_empty():
    assert layout.gauge(0, width=10) == "[░░░░░░░░░░] 0%"
    assert layout.gauge(100, width=10) == "[██████████] 100%"


def test_gauge_never_seen():
    assert layout.gauge(None, width=10) == "[----------] --"


def test_gauge_partial_rounds_to_nearest_block():
    text = layout.gauge(50, width=10)
    assert text == "[█████░░░░░] 50%"


def test_short_age_buckets():
    assert layout.short_age(now=100.0, wall_time=None) == "--"
    assert layout.short_age(now=100.2, wall_time=100.0) == "now"
    assert layout.short_age(now=115.0, wall_time=100.0) == "15s"
    assert layout.short_age(now=100.0 + 125, wall_time=100.0) == "2m"


def test_is_stale_threshold():
    assert layout.is_stale(now=100.0, wall_time=None) is False  # never-seen isn't "stale"
    assert layout.is_stale(now=110.0, wall_time=100.0, threshold=30) is False
    assert layout.is_stale(now=140.1, wall_time=100.0, threshold=30) is True


def test_strip_enum_suffix():
    assert layout.strip_enum_suffix("Discharging (2)") == "Discharging"
    assert layout.strip_enum_suffix("no parens here") == "no parens here"


def test_fit_pads_and_truncates():
    assert layout.fit("hi", 5) == "hi   "
    assert layout.fit("hello world", 5) == "hell…"
    assert layout.fit("x", 0) == ""
