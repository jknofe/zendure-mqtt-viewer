"""Integration tests using the real sample captures - offline, no network.

These exercise the full decode + state + layout path the way --replay does,
and pin down behavior on the deliberately-malformed line each capture file
contains.
"""
from __future__ import annotations

from pathlib import Path

from zendure_mqtt_viewer import layout
from zendure_mqtt_viewer.replay import replay_file
from zendure_mqtt_viewer.state import DashboardState

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _replay(filename: str) -> DashboardState:
    state = DashboardState()
    replay_file(SAMPLES_DIR / filename, state)
    return state


def test_sample_capture_1_has_exactly_one_malformed_line():
    state = _replay("sample_capture_1.jsonl")
    assert state.parse_errors == 1


def test_sample_capture_2_has_exactly_one_malformed_line():
    state = _replay("sample_capture_2.jsonl")
    assert state.parse_errors == 1


def test_replay_never_crashes_and_counts_all_lines():
    total_lines = sum(1 for _ in (SAMPLES_DIR / "sample_capture_1.jsonl").open())
    state = _replay("sample_capture_1.jsonl")
    # every line is either a successful message or a counted parse error
    assert state.messages_received + state.parse_errors == total_lines


def test_replay_populates_hub_power_state_and_settings():
    state = _replay("sample_capture_1.jsonl")
    assert "outputHomePower" in state.hub
    assert "electricLevel" in state.hub
    assert "minSoc" in state.hub or "socSet" in state.hub


def test_replay_populates_pack_data():
    state = _replay("sample_capture_1.jsonl")
    assert state.pack_order, "expected at least one battery pack"
    sn = state.pack_order[0]
    assert sn == "ZZ0EXAMPLE00001"
    assert "socLevel" in state.packs[sn] or "power" in state.packs[sn]


def test_replay_captures_cycle_counters_as_undecoded():
    state = _replay("sample_capture_1.jsonl")
    cycle_fields = {k for k in state.undecoded if k.endswith("Cycle")}
    assert cycle_fields, "expected at least one *Cycle field in undecoded"


def test_overview_dashboard_shows_battery_and_power_flow_panels():
    state = _replay("sample_capture_1.jsonl")
    frame = layout.build_frame(state, "overview", 80, 24, now=1784980800.0, mode="replay")
    text = frame.to_text()
    assert "BATTERY" in text
    assert "POWER FLOW" in text
    assert "HUB" in text


def test_packs_dashboard_shows_table_with_pack_row():
    state = _replay("sample_capture_1.jsonl")
    frame = layout.build_frame(state, "packs", 80, 24, now=1784980800.0, mode="replay")
    text = frame.to_text()
    assert "ZZ0EXAMPLE00001" in text
    assert "SoC" in text and "SoH" in text


def test_raw_dashboard_shows_cycle_counters():
    state = _replay("sample_capture_1.jsonl")
    frame = layout.build_frame(state, "raw", 80, 24, now=1784980800.0, mode="replay")
    text = frame.to_text()
    assert "Cycle" in text


def test_dashboard_never_shows_never_seen_field_as_zero():
    state = DashboardState()
    frame = layout.build_frame(state, "hub", 80, 24, now=1.0, mode="replay")
    text = frame.to_text()
    assert "0 W" not in text
    assert "--" in text


def test_frame_fits_narrow_terminal_without_crashing():
    state = _replay("sample_capture_1.jsonl")
    frame = layout.build_frame(state, "overview", 60, 20, now=1784980800.0, mode="replay")
    for line in frame.lines:
        assert sum(len(sp.text) for sp in line) <= 60
    assert len(frame.lines) <= 20
