"""Pure decoding logic for Zendure SolarFlow MQTT payloads.

Nothing in this module touches the network, the filesystem, or a terminal.
Every function here is a plain value-in/string-out transform, which is what
makes it unit-testable without a broker (see ``tests/test_decode.py``).

Scaling and enum meanings are taken from PROTOCOL.md. Where real captured
data contradicted the documented scaling (see ``soh`` below) the decoder
follows the data, not the doc comment, and that discrepancy is called out
in the README.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

# The hub reports this exact value for "remaining time" fields when the
# real answer is unknown/effectively infinite (e.g. output limit is 0, or
# input is fully charged). It must never be rendered as "999 minutes".
REMAIN_TIME_UNKNOWN_SENTINEL = 59940


# ---------------------------------------------------------------------------
# Enum tables (PROTOCOL.md "Fields" section)
# ---------------------------------------------------------------------------

PACK_STATE = {0: "Standby", 1: "Charging", 2: "Discharging"}
PASS_MODE = {0: "Auto", 1: "Off", 2: "On"}
HUB_STATE = {0: "Standby", 1: "Shutdown"}
WIFI_STATE = {0: "Offline", 1: "Online"}
HEAT_STATE = {0: "Inactive", 1: "Active"}
BYPASS_ACTIVE = {0: "Inactive", 1: "Active"}
ON_OFF = {0: "Off", 1: "On"}


# ---------------------------------------------------------------------------
# Scalar formatters. Each takes the raw JSON value and returns the string to
# display. They must never raise on the value types actually seen on the
# wire (int/float/str/bool/None) - decode_property() catches anything else.
# ---------------------------------------------------------------------------


def fmt_raw(value: Any) -> str:
    """No known decoding - show exactly what the hub sent."""
    return str(value)


def fmt_watts(value: Any) -> str:
    return f"{value} W"


def fmt_percent_direct(value: Any) -> str:
    return f"{value} %"


def fmt_percent_div10(value: Any) -> str:
    return f"{value / 10:.1f} %"


def fmt_volts_div100(value: Any) -> str:
    return f"{value / 100:.2f} V"


def fmt_celsius_from_decikelvin(value: Any) -> str:
    return f"{value * 0.1 - 273.15:.1f} °C"


def fmt_minutes_with_sentinel(value: Any) -> str:
    if value == REMAIN_TIME_UNKNOWN_SENTINEL:
        return "unknown/infinite"
    return f"{value} min"


def fmt_int(value: Any) -> str:
    return str(value)


def make_enum_formatter(mapping: dict[int, str]) -> Callable[[Any], str]:
    def _format(value: Any) -> str:
        label = mapping.get(value)
        if label is None:
            return f"Unknown({value!r})"
        return f"{label} ({value})"

    return _format


fmt_pack_state = make_enum_formatter(PACK_STATE)
fmt_pass_mode = make_enum_formatter(PASS_MODE)
fmt_hub_state = make_enum_formatter(HUB_STATE)
fmt_wifi_state = make_enum_formatter(WIFI_STATE)
fmt_heat_state = make_enum_formatter(HEAT_STATE)
fmt_bypass_active = make_enum_formatter(BYPASS_ACTIVE)
fmt_on_off = make_enum_formatter(ON_OFF)


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------

SECTION_POWER = "power"
SECTION_HUB_STATE = "hub_state"
SECTION_HUB_SETTINGS = "hub_settings"
SECTION_UNDECODED = "undecoded"


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    section: str
    formatter: Callable[[Any], str] = fmt_raw
    note: str = ""

    def format(self, raw: Any) -> str:
        try:
            return self.formatter(raw)
        except (TypeError, ValueError, ZeroDivisionError):
            return f"{raw!r} (decode error)"


# Hub-level "properties" fields, keyed by the JSON property name.
HUB_FIELD_SPECS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in [
        # -- power (Watts) --
        FieldSpec("solarInputPower", "Solar In (total)", SECTION_POWER, fmt_watts),
        FieldSpec("solarPower1", "Solar In (string 1)", SECTION_POWER, fmt_watts),
        FieldSpec("solarPower2", "Solar In (string 2)", SECTION_POWER, fmt_watts),
        # Zendure names these from the hub's point of view: the hub *outputs*
        # into the pack when charging, and the pack is an *input* to the hub
        # when discharging. Confirmed against packState in samples/.
        FieldSpec("packInputPower", "Battery Discharging", SECTION_POWER, fmt_watts),
        FieldSpec("outputPackPower", "Battery Charging", SECTION_POWER, fmt_watts),
        FieldSpec("outputHomePower", "Output to Home", SECTION_POWER, fmt_watts),
        FieldSpec("gridPower", "Grid Power", SECTION_POWER, fmt_watts, note="meaning unconfirmed"),
        FieldSpec("smartPower", "Smart Power", SECTION_POWER, fmt_watts, note="meaning unconfirmed"),
        # -- hub state --
        FieldSpec("electricLevel", "State of Charge", SECTION_HUB_STATE, fmt_percent_direct),
        FieldSpec("packState", "Pack State", SECTION_HUB_STATE, fmt_pack_state),
        FieldSpec("pass", "Bypass", SECTION_HUB_STATE, fmt_bypass_active),
        FieldSpec("hubState", "Hub State", SECTION_HUB_STATE, fmt_hub_state),
        FieldSpec("wifiState", "WiFi", SECTION_HUB_STATE, fmt_wifi_state),
        FieldSpec("heatState", "Heater", SECTION_HUB_STATE, fmt_heat_state),
        FieldSpec("packNum", "Pack Count", SECTION_HUB_STATE, fmt_int),
        FieldSpec("remainOutTime", "Discharge Time Left", SECTION_HUB_STATE, fmt_minutes_with_sentinel),
        FieldSpec("remainInputTime", "Charge Time Left", SECTION_HUB_STATE, fmt_minutes_with_sentinel),
        # -- hub settings --
        FieldSpec("outputLimit", "Output Limit", SECTION_HUB_SETTINGS, fmt_watts),
        FieldSpec("inputLimit", "Input Limit", SECTION_HUB_SETTINGS, fmt_watts),
        FieldSpec("inverseMaxPower", "Inverter Max Power", SECTION_HUB_SETTINGS, fmt_watts),
        FieldSpec("minSoc", "Min SoC (floor)", SECTION_HUB_SETTINGS, fmt_percent_div10),
        FieldSpec("socSet", "Target SoC", SECTION_HUB_SETTINGS, fmt_percent_div10),
        FieldSpec("passMode", "Bypass Mode", SECTION_HUB_SETTINGS, fmt_pass_mode),
        FieldSpec("autoModel", "Auto Model", SECTION_HUB_SETTINGS, fmt_raw, note="enum meaning vague"),
        FieldSpec("smartMode", "Smart Mode", SECTION_HUB_SETTINGS, fmt_raw, note="enum meaning vague"),
        FieldSpec("masterSwitch", "Master Switch", SECTION_HUB_SETTINGS, fmt_on_off),
        FieldSpec("buzzerSwitch", "Buzzer", SECTION_HUB_SETTINGS, fmt_on_off),
        FieldSpec("autoRecover", "Auto Recover", SECTION_HUB_SETTINGS, fmt_on_off),
        FieldSpec("inputMode", "Input Mode", SECTION_HUB_SETTINGS, fmt_raw, note="meaning unconfirmed"),
        FieldSpec("pvBrand", "PV Brand", SECTION_HUB_SETTINGS, fmt_raw, note="meaning unconfirmed"),
        FieldSpec("blueOta", "Bluetooth OTA", SECTION_HUB_SETTINGS, fmt_raw, note="meaning unconfirmed"),
        FieldSpec("masterSoftVersion", "Soft Version", SECTION_HUB_SETTINGS, fmt_raw),
        FieldSpec("masterhaerVersion", "Hard Version", SECTION_HUB_SETTINGS, fmt_raw),
    ]
}

# The five "*Cycle" counters: no documentation found anywhere. Always show
# raw in the Undecoded section, never invent a meaning for them.
KNOWN_UNDOCUMENTED_FIELDS = {
    "outputHomePowerCycle",
    "packInputPowerCycle",
    "outputPackPowerCycle",
    "solarPower1Cycle",
    "solarPower2Cycle",
}

# Top-level (not under "properties") fields that carry live device info and
# are worth tracking with the same last-seen/staleness treatment.
TOP_LEVEL_INFO_FIELDS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in [
        FieldSpec("wifiName", "WiFi SSID", SECTION_HUB_STATE, fmt_raw),
        FieldSpec("mac", "MAC Address", SECTION_HUB_STATE, fmt_raw),
        FieldSpec("ip", "IP Address", SECTION_HUB_STATE, fmt_raw),
    ]
}

# Keys at the top level of every payload that are protocol envelope, not
# data to display as a "field".
ENVELOPE_KEYS = {"messageId", "product", "deviceId", "timestamp", "properties", "packData"}


# ---------------------------------------------------------------------------
# packData (per battery pack) fields
# ---------------------------------------------------------------------------

PACK_FIELD_SPECS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in [
        FieldSpec("socLevel", "SoC", "pack", fmt_percent_direct),
        # PROTOCOL.md documents `soh` as a direct percent. Real captures only
        # ever show soh=978, which as a direct percent is nonsensical (State
        # of Health can't be 978%). 978/10 = 97.8%, a plausible SoH reading,
        # so this decoder treats it like the other x10-scaled percent fields
        # and flags the discrepancy here and in the README.
        FieldSpec("soh", "State of Health", "pack", fmt_percent_div10, note="doc says direct %, data implies /10"),
        FieldSpec("maxTemp", "Max Cell Temp", "pack", fmt_celsius_from_decikelvin),
        FieldSpec("maxVol", "Max Cell Voltage", "pack", fmt_volts_div100),
        FieldSpec("minVol", "Min Cell Voltage", "pack", fmt_volts_div100),
        FieldSpec("totalVol", "Pack Voltage", "pack", fmt_volts_div100, note="/100 presumed"),
        FieldSpec("power", "Power", "pack", fmt_watts),
        FieldSpec("state", "State", "pack", fmt_pack_state, note="reuses hub packState enum, presumed"),
        FieldSpec("softVersion", "Firmware", "pack", fmt_raw),
    ]
}

PACK_KEY_FIELD = "sn"
