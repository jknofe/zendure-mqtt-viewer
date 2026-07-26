"""The one message this tool may send, and everything that must still be refused.

`soh`, `maxTemp` and `softVersion` only arrive in a full report, which the hub
sends when asked. Asking means publishing - so it is armed by value: the
client holds one exact (topic, payload) pair and refuses anything else, and
publish() still raises for every caller. These tests pin that the hole is
exactly one message wide.

No network: paho's publish is stubbed and the callbacks are driven directly.
"""
from __future__ import annotations

import paho.mqtt.client as mqtt
import pytest

from zendure_mqtt_viewer import layout
from zendure_mqtt_viewer.config import BrokerConfig
from zendure_mqtt_viewer.mqtt_client import (
    REFRESH_MIN_INTERVAL,
    REFRESH_PAYLOAD,
    PublishForbiddenError,
    Subscriber,
)
from zendure_mqtt_viewer.state import DashboardState

CFG = BrokerConfig(
    host="broker.invalid", port=1883, username=None, password=None,
    product_id="73bkTV", device_id="AB1234CD",
)
READ_TOPIC = "iot/73bkTV/AB1234CD/properties/read"
WRITE_TOPIC = "iot/73bkTV/AB1234CD/properties/write"


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have gone to the broker."""
    calls = []

    def fake_publish(self, topic, payload=None, qos=0, retain=False, **kw):
        calls.append((topic, payload, qos, retain))
        return mqtt.MQTTMessageInfo(1)

    monkeypatch.setattr(mqtt.Client, "publish", fake_publish)
    return calls


def _connected():
    state = DashboardState()
    state.note_connected()
    return Subscriber(CFG, state), state


# ---------------------------------------------------------------------------
# The topic is not derivable from the report topic - getting it wrong is silent
# ---------------------------------------------------------------------------


def test_the_read_topic_is_the_iot_form_not_the_report_form():
    assert CFG.report_topic == "/73bkTV/AB1234CD/properties/report"
    assert CFG.read_topic == READ_TOPIC


def test_the_request_goes_to_the_read_topic_with_the_exact_payload(sent):
    sub, _ = _connected()
    assert sub.request_full_report(now=1000.0) is True
    assert sent == [(READ_TOPIC, REFRESH_PAYLOAD, 0, False)]
    assert REFRESH_PAYLOAD == '{"properties": ["getAll"]}'


# ---------------------------------------------------------------------------
# The hole is exactly one message wide
# ---------------------------------------------------------------------------


def test_publish_still_raises_for_every_caller(sent):
    sub, _ = _connected()
    with pytest.raises(PublishForbiddenError):
        sub._client.publish(READ_TOPIC, REFRESH_PAYLOAD)
    assert sent == []


def test_the_write_topic_is_refused_even_through_the_armed_path(sent):
    sub, _ = _connected()
    with pytest.raises(PublishForbiddenError):
        sub._client.send_allowed_request(WRITE_TOPIC, '{"properties":{"outputLimit":800}}')
    assert sent == []


@pytest.mark.parametrize(
    "topic,payload",
    [
        (READ_TOPIC, '{"properties":["getAll"]}'),        # different spacing
        (READ_TOPIC, '{"properties": ["getall"]}'),       # different case
        (READ_TOPIC, '{"properties": ["getAll", "x"]}'),  # extra element
        ("iot/73bkTV/OTHER/properties/read", REFRESH_PAYLOAD),
        (READ_TOPIC.rstrip("d"), REFRESH_PAYLOAD),
    ],
)
def test_anything_but_the_armed_pair_is_refused(sent, topic, payload):
    sub, _ = _connected()
    with pytest.raises(PublishForbiddenError):
        sub._client.send_allowed_request(topic, payload)
    assert sent == []


def test_will_set_is_still_refused(sent):
    sub, _ = _connected()
    with pytest.raises(PublishForbiddenError):
        sub._client.will_set(READ_TOPIC, REFRESH_PAYLOAD)


# ---------------------------------------------------------------------------
# Arming is per client, and only Subscriber does it
# ---------------------------------------------------------------------------


def test_an_unarmed_client_cannot_publish_at_all(sent):
    # The arming is what makes the one message possible; a client built
    # without it (anything but Subscriber) has no publish path whatsoever.
    from zendure_mqtt_viewer.mqtt_client import GuardedMqttClient

    client = GuardedMqttClient(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    with pytest.raises(PublishForbiddenError):
        client.send_allowed_request(READ_TOPIC, REFRESH_PAYLOAD)
    assert sent == []


# ---------------------------------------------------------------------------
# "Not now" is a return value, never an exception - the caller is a keypress
# ---------------------------------------------------------------------------


def test_a_second_request_inside_the_rate_limit_is_dropped(sent):
    sub, _ = _connected()
    assert sub.request_full_report(now=1000.0) is True
    assert sub.request_full_report(now=1000.0 + REFRESH_MIN_INTERVAL - 0.1) is False
    assert sub.request_full_report(now=1000.0 + REFRESH_MIN_INTERVAL) is True
    assert len(sent) == 2


def test_a_request_while_disconnected_is_dropped(sent):
    sub, state = _connected()
    state.note_connection_error("disconnected: Unspecified error")
    assert sub.request_full_report(now=1000.0) is False
    assert sent == []


def test_a_broker_error_is_swallowed_and_logged(monkeypatch, sent):
    sub, _ = _connected()

    def boom(self, *a, **kw):
        raise OSError("socket gone")

    monkeypatch.setattr(mqtt.Client, "publish", boom)
    assert sub.request_full_report(now=1000.0) is False
    # and the rate limiter is not consumed by a failure
    monkeypatch.setattr(mqtt.Client, "publish", lambda self, *a, **kw: sent.append(a) or mqtt.MQTTMessageInfo(1))
    assert sub.request_full_report(now=1000.5) is True


# ---------------------------------------------------------------------------
# What the screen says about it
# ---------------------------------------------------------------------------


def _status(state, now=1000.0, mode="live"):
    frame = layout.build_frame(state, "overview", 100, 27, now, mode=mode)
    return frame.to_text().splitlines()[-2]


def test_the_key_hint_is_offered_in_live_mode():
    state = DashboardState()
    assert "[r] refresh" in _status(state)
    assert "[q] quit" in _status(state)


def test_the_status_bar_acknowledges_a_request_then_goes_back(sent):
    sub, state = _connected()
    sub.request_full_report(now=1000.0)
    assert "refresh sent" in _status(state, now=1000.5)
    assert "refresh sent" in _status(state, now=1003.9)
    assert "refresh sent" not in _status(state, now=1004.1)
    assert "[r] refresh" in _status(state, now=1004.1)


def test_replay_mode_never_offers_refresh():
    state = DashboardState()
    assert "[r] refresh" not in _status(state, mode="replay")


def test_the_acknowledgement_does_not_change_the_frame_geometry(sent):
    sub, state = _connected()
    sub.request_full_report(now=1000.0)
    for cols, rows in [(54, 14), (80, 24), (100, 27)]:
        frame = layout.build_frame(state, "overview", cols, rows, 1000.5, mode="live")
        assert frame.line_widths_ok()
        assert len(frame.lines) == rows
