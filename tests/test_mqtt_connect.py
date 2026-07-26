"""Connection outcome handling.

paho invokes on_connect for *refused* connections too, handing over a
failure reason code. The bug this pins: treating that as a success left the
dashboard drawing a confident CONNECTED header while it was subscribed to
nothing and would never receive a message. A refusal has to be visible.

No network connection is made in these tests - the paho callbacks are
invoked directly with the arguments paho would pass.
"""
from __future__ import annotations

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.reasoncodes import ReasonCode

from zendure_mqtt_viewer import layout
from zendure_mqtt_viewer.config import BrokerConfig
from zendure_mqtt_viewer.mqtt_client import Subscriber, _is_failure
from zendure_mqtt_viewer.state import DashboardState

CONNACK = mqtt.CONNACK >> 4
DISCONNECT = mqtt.DISCONNECT >> 4

UNSPECIFIED_ERROR = 0x80
NOT_AUTHORIZED = 0x87
SUCCESS = 0x00


class FakeClient:
    """Just enough of a paho client for the callbacks under test."""

    def __init__(self, subscribe_result: int = mqtt.MQTT_ERR_SUCCESS) -> None:
        self.subscribe_result = subscribe_result
        self.subscriptions: list[str] = []

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        return (self.subscribe_result, 1)


def _subscriber() -> tuple[Subscriber, DashboardState]:
    state = DashboardState()
    cfg = BrokerConfig(host="broker.invalid", port=1883, username=None, password=None)
    return Subscriber(cfg, state), state


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [UNSPECIFIED_ERROR, NOT_AUTHORIZED])
def test_refused_connect_does_not_report_connected(code):
    sub, state = _subscriber()
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=code))
    assert state.connected is False


def test_refused_connect_does_not_subscribe():
    sub, state = _subscriber()
    client = FakeClient()
    sub._handle_connect(client, None, {}, ReasonCode(CONNACK, identifier=UNSPECIFIED_ERROR))
    assert client.subscriptions == []


def test_refused_connect_records_the_broker_s_reason():
    sub, state = _subscriber()
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=UNSPECIFIED_ERROR))
    assert "Unspecified error" in state.connection_error


def test_successful_connect_still_connects_and_subscribes():
    sub, state = _subscriber()
    client = FakeClient()
    sub._handle_connect(client, None, {}, ReasonCode(CONNACK, identifier=SUCCESS))
    assert state.connected is True
    assert state.connection_error == ""
    assert client.subscriptions == [sub.config.report_topic]


def test_failed_subscribe_is_not_silently_swallowed():
    sub, state = _subscriber()
    client = FakeClient(subscribe_result=mqtt.MQTT_ERR_NO_CONN)
    sub._handle_connect(client, None, {}, ReasonCode(CONNACK, identifier=SUCCESS))
    assert state.connected is False
    assert "subscribe" in state.connection_error


def test_a_later_good_connect_clears_the_previous_error():
    sub, state = _subscriber()
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=UNSPECIFIED_ERROR))
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=SUCCESS))
    assert state.connected is True
    assert state.connection_error == ""


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


def test_unexpected_disconnect_is_recorded():
    sub, state = _subscriber()
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=SUCCESS))
    sub._handle_disconnect(FakeClient(), None, {}, ReasonCode(DISCONNECT, identifier=UNSPECIFIED_ERROR))
    assert state.connected is False
    assert "Unspecified error" in state.connection_error


def test_clean_disconnect_is_not_an_error():
    sub, state = _subscriber()
    sub._handle_connect(FakeClient(), None, {}, ReasonCode(CONNACK, identifier=SUCCESS))
    sub._handle_disconnect(FakeClient(), None, {}, ReasonCode(DISCONNECT, identifier=SUCCESS))
    assert state.connected is False
    assert state.connection_error == ""


# ---------------------------------------------------------------------------
# _is_failure tolerates whatever paho hands it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (ReasonCode(CONNACK, identifier=SUCCESS), False),
        (ReasonCode(CONNACK, identifier=UNSPECIFIED_ERROR), True),
        (0, False),
        (5, True),
        (None, False),
        ("nonsense", False),
    ],
)
def test_is_failure(value, expected):
    assert _is_failure(value) is expected


# ---------------------------------------------------------------------------
# The layout must NOT show it
#
# The reason string belongs in the log file. On screen a refusal shows up as
# link state (DISCONNECTED) and nothing else - error text on a fixed
# one-screen layout reads as damage.
# ---------------------------------------------------------------------------


def _text(state: DashboardState) -> str:
    frame = layout.build_frame(state, "overview", 100, 27, now=1784980800.0, mode="live")
    return "\n".join("".join(sp.text for sp in line) for line in frame.lines)


@pytest.mark.parametrize("tab", layout.TABS)
def test_connection_error_text_never_reaches_the_screen(tab):
    state = DashboardState()
    state.note_connection_error("connect refused: Unspecified error")
    frame = layout.build_frame(state, tab, 100, 27, now=1784980800.0, mode="live")
    text = "\n".join("".join(sp.text for sp in line) for line in frame.lines)
    assert "Unspecified error" in state.connection_error  # recorded...
    assert "Unspecified error" not in text  # ...but not drawn
    assert "refused" not in text


def test_a_refusal_still_shows_as_disconnected():
    state = DashboardState()
    state.note_connection_error("connect refused: Unspecified error")
    text = _text(state)
    assert "DISCONNECTED" in text
    assert "CONNECTED" not in text.replace("DISCONNECTED", "")


def test_the_status_bar_keeps_its_normal_content_during_an_outage():
    # The reason string used to take the whole row over, so the message
    # count, error count and key hints vanished exactly when they mattered.
    state = DashboardState()
    state.note_connection_error("connect refused: Unspecified error")
    text = _text(state)
    assert "msgs" in text
    assert "errors" in text
    assert "quit" in text


def test_a_bad_payload_does_not_clear_a_healthy_connection():
    # Parse errors are about message content, not the link.
    state = DashboardState()
    state.note_connected()
    state.note_parse_error("invalid JSON: whatever")
    assert state.connected is True
    assert state.connection_error == ""
    assert "CONNECTED" in _text(state)
