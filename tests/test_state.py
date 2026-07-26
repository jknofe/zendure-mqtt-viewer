"""Unit tests for DashboardState: delta-merge, staleness, undecoded capture."""
from __future__ import annotations

import pytest

from zendure_mqtt_viewer import decode
from zendure_mqtt_viewer.state import (
    DashboardState,
    MalformedMessageError,
    parse_line,
)


def test_parse_line_valid_json_object():
    payload = parse_line('{"messageId":"1","properties":{"a":1}}')
    assert payload["properties"]["a"] == 1


def test_parse_line_rejects_non_json():
    with pytest.raises(MalformedMessageError):
        parse_line("Timed out")


def test_parse_line_rejects_empty():
    with pytest.raises(MalformedMessageError):
        parse_line("   \n")


def test_parse_line_rejects_json_scalar_not_object():
    with pytest.raises(MalformedMessageError):
        parse_line("42")


def test_parse_line_rejects_json_array():
    with pytest.raises(MalformedMessageError):
        parse_line("[1,2,3]")


# ---------------------------------------------------------------------------
# Never-seen vs zero
# ---------------------------------------------------------------------------


def test_never_seen_field_is_absent_not_zero():
    state = DashboardState()
    assert "outputHomePower" not in state.hub


def test_a_field_reported_as_zero_is_recorded_as_zero_not_absent():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": {"outputPackPower": 0}}, wall_time=100.0)
    assert "outputPackPower" in state.hub
    assert state.hub["outputPackPower"].raw == 0
    assert state.hub["outputPackPower"].display == "0 W"


# ---------------------------------------------------------------------------
# Delta merge: one field must not clear the others
# ---------------------------------------------------------------------------


def test_delta_merge_keeps_previously_seen_fields():
    state = DashboardState()
    state.apply_payload(
        {"timestamp": 1, "properties": {"packInputPower": 550, "outputHomePower": 607}},
        wall_time=100.0,
    )
    state.apply_payload({"timestamp": 2, "properties": {"packInputPower": 551}}, wall_time=101.0)

    assert state.hub["packInputPower"].raw == 551  # updated
    assert state.hub["outputHomePower"].raw == 607  # untouched, still present
    assert state.hub["outputHomePower"].wall_time == 100.0  # age reflects original message


def test_delta_merge_across_many_messages_accumulates_full_state():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": {"electricLevel": 76}}, wall_time=1.0)
    state.apply_payload({"timestamp": 2, "properties": {"minSoc": 200}}, wall_time=2.0)
    state.apply_payload({"timestamp": 3, "properties": {"socSet": 1000}}, wall_time=3.0)

    assert state.hub["electricLevel"].display == "76 %"
    assert state.hub["minSoc"].display == "20.0 %"
    assert state.hub["socSet"].display == "100.0 %"


# ---------------------------------------------------------------------------
# Malformed message handling
# ---------------------------------------------------------------------------


def test_note_parse_error_increments_counter_and_never_raises():
    state = DashboardState()
    state.note_parse_error("boom")
    state.note_parse_error("boom again")
    assert state.parse_errors == 2
    # a parse error must not touch messages_received or any field
    assert state.messages_received == 0
    assert state.hub == {}


# ---------------------------------------------------------------------------
# Unknown field capture (undecoded section)
# ---------------------------------------------------------------------------


def test_known_undocumented_cycle_field_goes_to_undecoded():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": {"solarPower1Cycle": 38}}, wall_time=1.0)
    assert "solarPower1Cycle" in state.undecoded
    assert state.undecoded["solarPower1Cycle"].display == "38"
    assert "solarPower1Cycle" not in state.hub


def test_totally_unknown_property_field_is_surfaced_not_dropped():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "properties": {"brandNewFirmwareField": 42}}, wall_time=1.0)
    assert "brandNewFirmwareField" in state.undecoded
    assert state.undecoded["brandNewFirmwareField"].raw == 42
    assert state.undecoded["brandNewFirmwareField"].note == "properties"


def test_unknown_top_level_key_is_surfaced():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "somethingNew": "x"}, wall_time=1.0)
    assert "somethingNew" in state.undecoded
    assert state.undecoded["somethingNew"].note == "top-level"


def test_unknown_packdata_field_is_surfaced_with_pack_prefix():
    state = DashboardState()
    state.apply_payload(
        {"timestamp": 1, "packData": [{"sn": "ABC123", "brandNewPackField": 7}]},
        wall_time=1.0,
    )
    assert "packData.ABC123.brandNewPackField" in state.undecoded


# ---------------------------------------------------------------------------
# Battery pack grouping
# ---------------------------------------------------------------------------


def test_pack_data_grouped_by_sn():
    state = DashboardState()
    state.apply_payload(
        {"timestamp": 1, "packData": [{"sn": "SN1", "socLevel": 80, "power": 100}]},
        wall_time=1.0,
    )
    state.apply_payload(
        {"timestamp": 2, "packData": [{"sn": "SN1", "power": 150}]},
        wall_time=2.0,
    )
    assert state.packs["SN1"]["socLevel"].raw == 80  # untouched by second message
    assert state.packs["SN1"]["power"].raw == 150  # updated
    assert state.pack_order == ["SN1"]


def test_pack_data_entry_without_sn_is_not_dropped_silently():
    state = DashboardState()
    state.apply_payload({"timestamp": 1, "packData": [{"power": 100}]}, wall_time=1.0)
    assert state.packs == {}
    assert "packData[no-sn]" in state.undecoded


def test_remain_time_sentinel_round_trips_through_state():
    state = DashboardState()
    state.apply_payload(
        {"timestamp": 1, "properties": {"remainInputTime": decode.REMAIN_TIME_UNKNOWN_SENTINEL}},
        wall_time=1.0,
    )
    assert state.hub["remainInputTime"].display == "unknown/infinite"
