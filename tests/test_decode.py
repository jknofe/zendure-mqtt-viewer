"""Unit tests for the pure decoding functions in zendure_mqtt_viewer.decode."""
from __future__ import annotations

from zendure_mqtt_viewer import decode


# ---------------------------------------------------------------------------
# Scaling conversions
# ---------------------------------------------------------------------------


def test_watts_direct():
    assert decode.fmt_watts(584) == "584 W"
    assert decode.fmt_watts(0) == "0 W"


def test_percent_direct():
    assert decode.fmt_percent_direct(76) == "76 %"


def test_percent_div10_minsoc():
    # 200 -> 20% floor, per PROTOCOL.md example
    assert decode.fmt_percent_div10(200) == "20.0 %"


def test_percent_div10_socset():
    # 1000 -> 100% target, per PROTOCOL.md example
    assert decode.fmt_percent_div10(1000) == "100.0 %"


def test_volts_div100():
    assert decode.fmt_volts_div100(329) == "3.29 V"


def test_celsius_from_decikelvin():
    # observed in sample captures: 3071 deciK -> ~34.0C, 3081 -> ~35.0C
    assert decode.fmt_celsius_from_decikelvin(3071) == "34.0 °C"
    assert decode.fmt_celsius_from_decikelvin(3081) == "35.0 °C"


def test_celsius_from_decikelvin_freezing():
    # 273.15 K = 0 C -> raw deciKelvin value 2731.5, use 2732 for a clean int
    assert decode.fmt_celsius_from_decikelvin(2732) == "0.1 °C"


# ---------------------------------------------------------------------------
# remainOutTime / remainInputTime sentinel
# ---------------------------------------------------------------------------


def test_minutes_normal_value():
    assert decode.fmt_minutes_with_sentinel(196) == "196 min"


def test_minutes_sentinel_is_not_a_real_duration():
    text = decode.fmt_minutes_with_sentinel(decode.REMAIN_TIME_UNKNOWN_SENTINEL)
    assert text == "unknown/infinite"
    assert "59940" not in text


def test_sentinel_constant_value():
    assert decode.REMAIN_TIME_UNKNOWN_SENTINEL == 59940


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_pack_state_enum():
    assert decode.fmt_pack_state(0) == "Standby (0)"
    assert decode.fmt_pack_state(1) == "Charging (1)"
    assert decode.fmt_pack_state(2) == "Discharging (2)"


def test_pack_state_unknown_value_does_not_crash():
    assert decode.fmt_pack_state(99) == "Unknown(99)"


def test_pass_mode_enum():
    assert decode.fmt_pass_mode(0) == "Auto (0)"
    assert decode.fmt_pass_mode(1) == "Off (1)"
    assert decode.fmt_pass_mode(2) == "On (2)"


def test_hub_state_enum():
    assert decode.fmt_hub_state(0) == "Standby (0)"
    assert decode.fmt_hub_state(1) == "Shutdown (1)"


def test_wifi_state_enum():
    assert decode.fmt_wifi_state(0) == "Offline (0)"
    assert decode.fmt_wifi_state(1) == "Online (1)"


def test_heat_state_enum():
    assert decode.fmt_heat_state(0) == "Inactive (0)"
    assert decode.fmt_heat_state(1) == "Active (1)"


def test_bypass_active_enum():
    assert decode.fmt_bypass_active(0) == "Inactive (0)"
    assert decode.fmt_bypass_active(1) == "Active (1)"


def test_on_off_enum():
    assert decode.fmt_on_off(0) == "Off (0)"
    assert decode.fmt_on_off(1) == "On (1)"


# ---------------------------------------------------------------------------
# Field registry sanity
# ---------------------------------------------------------------------------


# The five "*Cycle" counters: no documentation found anywhere. They must fall
# through to the Undecoded section rather than be given an invented meaning.
UNDOCUMENTED_CYCLE_FIELDS = {
    "outputHomePowerCycle",
    "packInputPowerCycle",
    "outputPackPowerCycle",
    "solarPower1Cycle",
    "solarPower2Cycle",
}


def test_undocumented_cycle_fields_are_not_in_the_decoded_registry():
    for key in UNDOCUMENTED_CYCLE_FIELDS:
        assert key not in decode.HUB_FIELD_SPECS


def test_fieldspec_format_never_raises_on_decode_error():
    spec = decode.FieldSpec("x", "X", decode.SECTION_POWER, decode.fmt_volts_div100)
    # dividing a string would normally raise TypeError
    result = spec.format("not-a-number")
    assert "decode error" in result


def test_soh_uses_div10_scaling_per_observed_data():
    # PROTOCOL.md documents soh as a direct percent, but the only observed
    # value in real captures is 978, which only makes sense as 97.8%.
    spec = decode.PACK_FIELD_SPECS["soh"]
    assert spec.format(978) == "97.8 %"
