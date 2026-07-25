"""Safety-critical test: the MQTT client used by this tool must be
physically incapable of publishing to the broker.

This is a required deliverable per the tool's safety rule, not a nice to
have: the write topic commands a real battery and inverter attached to a
house, and a stray publish there can dump the battery or cut power
delivery. No network connection is made in this test.
"""
from __future__ import annotations

import paho.mqtt.client as mqtt
import pytest

from zendure_mqtt_viewer.mqtt_client import GuardedMqttClient, PublishForbiddenError


def _make_client() -> GuardedMqttClient:
    return GuardedMqttClient(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)


def test_publish_forbidden_error_is_a_runtime_error():
    assert issubclass(PublishForbiddenError, RuntimeError)


def test_publish_raises_with_no_arguments():
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.publish()


def test_publish_raises_with_topic_only():
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.publish("iot/73bkTV/AB1234CD/properties/write")


def test_publish_raises_with_full_write_command_payload():
    # The exact shape of a real (forbidden) write attempt.
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.publish(
            "iot/73bkTV/AB1234CD/properties/write",
            payload='{"properties": {"outputLimit": 0}}',
            qos=0,
            retain=False,
        )


def test_publish_raises_even_with_empty_payload():
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.publish("iot/73bkTV/AB1234CD/properties/write", payload="")


def test_publish_raises_even_with_none_payload():
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.publish("iot/73bkTV/AB1234CD/properties/write", payload=None)


def test_will_set_also_raises():
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.will_set("iot/73bkTV/AB1234CD/properties/write", payload="{}")


def test_guarded_client_is_still_a_real_paho_client_for_subscribe_purposes():
    # subscribe() must remain usable - only writes are blocked.
    client = _make_client()
    assert isinstance(client, mqtt.Client)
    assert hasattr(client, "subscribe")
