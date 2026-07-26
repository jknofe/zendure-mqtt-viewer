"""Colour.

Two invariants matter more than which colour anything is:

  1. Colour lives entirely in Span.attr. Adding it must not move, pad, or
     re-word a single character - the one-screen guarantees and the
     plain-text --once output are unaffected.
  2. Attr strings are token sets, so a monochrome terminal (or a curses
     build that runs out of colour pairs) still renders every emphasis.
"""
from __future__ import annotations

import curses

import pytest

from zendure_mqtt_viewer import layout
from zendure_mqtt_viewer.state import DashboardState

T0 = 1784980800.0


def _state(props: dict) -> DashboardState:
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": props}, wall_time=T0)
    return state


def _spans(state: DashboardState, tab: str = "overview") -> list[layout.Span]:
    frame = layout.build_frame(state, tab, 100, 27, now=T0, mode="live")
    return [sp for line in frame.lines for sp in line]


def _attr_of(state: DashboardState, needle: str, tab: str = "overview") -> str:
    for sp in _spans(state, tab):
        if needle in sp.text:
            return sp.attr
    raise AssertionError(f"no span containing {needle!r}")


# ---------------------------------------------------------------------------
# Colour changes styling, never text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("percent", [None, 0, 19, 20, 50, 76, 100])
def test_gauge_spans_render_exactly_the_same_text_as_gauge(percent):
    spans = layout.gauge_spans(percent, width=20)
    assert "".join(sp.text for sp in spans) == layout.gauge(percent, width=20)


@pytest.mark.parametrize("tab", layout.TABS)
def test_every_span_attr_uses_known_tokens(tab):
    state = _state({"packState": 2, "packInputPower": 355, "electricLevel": 76})
    known = set(layout.COLORS) | set(layout.EMPHASIS)
    for sp in _spans(state, tab):
        unknown = set(sp.attr.split()) - known
        assert not unknown, f"unknown attr token(s) {unknown} on {sp.text!r}"


@pytest.mark.parametrize("tab", layout.TABS)
def test_at_most_one_colour_per_span(tab):
    # Two colour pairs on one span would be ambiguous - last one silently wins.
    state = _state({"packState": 1, "outputPackPower": 20, "electricLevel": 76})
    for sp in _spans(state, tab):
        colors = [t for t in sp.attr.split() if t in layout.COLORS]
        assert len(colors) <= 1, f"{sp.text!r} carries {colors}"


def test_colour_does_not_change_the_frame_geometry():
    state = _state({"electricLevel": 76, "packState": 2, "packInputPower": 355})
    for cols, rows in [(80, 24), (100, 30), (54, 14)]:
        frame = layout.build_frame(state, "overview", cols, rows, now=T0, mode="live")
        assert len(frame.lines) == rows
        for line in frame.lines:
            assert sum(len(sp.text) for sp in line) == cols


# ---------------------------------------------------------------------------
# The colours say something true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "percent,expected",
    [(None, "muted"), (0, "error"), (19, "error"), (20, "warn"), (49, "warn"), (50, "ok"), (100, "ok")],
)
def test_soc_colour_follows_the_charge_level(percent, expected):
    assert layout.soc_color(percent) == expected


def test_charging_is_green_and_discharging_is_yellow():
    charging = _attr_of(_state({"packState": 1, "outputPackPower": 20}), "Charging")
    discharging = _attr_of(_state({"packState": 2, "packInputPower": 355}), "Discharging")
    assert "ok" in charging.split()
    assert "warn" in discharging.split()


def test_idle_battery_is_not_coloured_as_activity():
    attr = _attr_of(_state({"packState": 0}), "Idle")
    assert not (set(attr.split()) & {"ok", "warn", "error"})


def test_connection_states_are_coloured_distinctly():
    live = DashboardState()
    live.note_connected()
    down = DashboardState()
    down.note_connection_error("connect refused: Unspecified error")

    assert layout._connection_attr(live, "live") == "bold ok"
    assert layout._connection_attr(down, "live") == "bold error"
    assert layout._connection_attr(DashboardState(), "replay") == "bold info"


def test_no_red_error_text_is_drawn_anywhere():
    # "error" colouring is allowed on the fixed DISCONNECTED marker and on
    # nothing else: a red reason string in the body is the thing that made
    # the dashboard look broken.
    state = DashboardState()
    state.note_connection_error("connect refused: Unspecified error")
    state.note_parse_error("invalid JSON: whatever")
    for tab in layout.TABS:
        frame = layout.build_frame(state, tab, 100, 27, now=T0, mode="live")
        for line in frame.lines:
            for sp in line:
                if "error" in sp.attr.split():
                    assert "DISCONNECTED" in sp.text, f"red text on screen: {sp.text!r}"


# ---------------------------------------------------------------------------
# curses attribute resolution
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_leaked_pairs():
    # These tests poke module-level colour state; keep them independent.
    from zendure_mqtt_viewer import tui

    tui._color_pairs.clear()
    tui._attr_cache.clear()
    yield
    tui._color_pairs.clear()
    tui._attr_cache.clear()


def test_emphasis_tokens_resolve_without_any_colour_support():
    from zendure_mqtt_viewer import tui

    assert tui.attr_for("bold") == curses.A_BOLD
    assert tui.attr_for("bold reverse") == (curses.A_BOLD | curses.A_REVERSE)
    assert tui.attr_for("muted") == curses.A_DIM


def test_colour_tokens_are_dropped_on_a_monochrome_terminal():
    # No colour pairs allocated: "bold ok" still has to come out bold.
    from zendure_mqtt_viewer import tui

    assert tui.attr_for("bold ok") == curses.A_BOLD


def test_colour_tokens_apply_when_pairs_exist():
    # curses.color_pair() needs a live screen, so stand in a plain bitmask -
    # attr_for only ORs whatever the pair table holds.
    from zendure_mqtt_viewer import tui

    fake_pair = 1 << 12
    tui._color_pairs["ok"] = fake_pair
    tui._attr_cache.clear()
    assert tui.attr_for("bold ok") == (curses.A_BOLD | fake_pair)


def test_unknown_tokens_are_ignored_rather_than_raising():
    from zendure_mqtt_viewer import tui

    assert tui.attr_for("bold chartreuse") == curses.A_BOLD
    assert tui.attr_for("") == curses.A_NORMAL


def test_every_colour_the_layout_uses_has_a_curses_mapping():
    from zendure_mqtt_viewer import tui

    assert set(layout.COLORS) == set(tui.COLOR_FG)
    assert set(layout.EMPHASIS) == set(tui.EMPHASIS_MAP)
