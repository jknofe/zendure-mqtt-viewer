"""MQTT I/O layer - read-only by construction.

SAFETY: this module must never publish to the broker. GuardedMqttClient
overrides publish()/will_set() to raise unconditionally, regardless of what
calls them or with what arguments. This is enforced in code, not just by
convention or by "we just don't call it" - see tests/test_mqtt_guard.py.

The write topic (`iot/.../properties/write`) commands a real battery and
inverter. This module only ever subscribes to the report topic.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import BrokerConfig
from .state import DashboardState, MalformedMessageError, parse_line

logger = logging.getLogger(__name__)


class PublishForbiddenError(RuntimeError):
    """Raised the instant anything tries to write to the broker."""


def _is_failure(reason_code) -> bool:
    """True if a paho reason code means "this did not work".

    paho v2 hands callbacks a ReasonCode object (``.is_failure``); be liberal
    about ints and None so a paho version change degrades to "nonzero is bad"
    rather than silently reporting every refusal as a success.
    """
    if reason_code is None:
        return False
    is_failure = getattr(reason_code, "is_failure", None)
    if isinstance(is_failure, bool):
        return is_failure
    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return False


class GuardedMqttClient(mqtt.Client):
    """A paho MQTT client with every broker-write path disabled.

    Subscribing is fine and is all this tool ever does. Publishing -
    including an empty payload, a retained-clear, or configuring a will
    message that the broker would publish on our behalf - is forbidden.
    """

    def publish(self, *args, **kwargs):  # type: ignore[override]
        raise PublishForbiddenError(
            "GuardedMqttClient.publish() was called. This tool is read-only "
            "and must never publish to the MQTT broker."
        )

    def will_set(self, *args, **kwargs):  # type: ignore[override]
        raise PublishForbiddenError(
            "GuardedMqttClient.will_set() was called. This tool is read-only "
            "and must never configure a will message."
        )


class Subscriber:
    """Owns the GuardedMqttClient, feeds messages into a DashboardState."""

    def __init__(
        self,
        config: BrokerConfig,
        state: DashboardState,
        on_update: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.state = state
        self.on_update = on_update

        self._client = GuardedMqttClient(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self) -> None:
        self._client.connect(self.config.host, self.config.port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.loop_stop()
        finally:
            self._client.disconnect()

    # -- paho callbacks (CallbackAPIVersion.VERSION2 signatures) --------

    def _handle_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        # paho calls on_connect for *refused* connections too, carrying a
        # failure reason code ("Unspecified error", "Not authorized", ...).
        # Treating that as connected leaves the dashboard claiming CONNECTED
        # while subscribing on a dead socket and never receiving a message.
        if _is_failure(reason_code):
            self.state.note_connection_error(f"connect refused: {reason_code}")
            logger.error("connect refused: %s", reason_code)
            if self.on_update:
                self.on_update()
            return

        result, _mid = client.subscribe(self.config.report_topic, qos=0)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.state.note_connection_error(
                f"subscribe to {self.config.report_topic} failed: {mqtt.error_string(result)}"
            )
            logger.error("subscribe failed: %s", mqtt.error_string(result))
        else:
            self.state.note_connected()
            logger.info("connected, subscribed to %s", self.config.report_topic)
        if self.on_update:
            self.on_update()

    def _handle_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        if _is_failure(reason_code):
            self.state.note_connection_error(f"disconnected: {reason_code}")
            logger.warning("disconnected: %s", reason_code)
        else:
            # Clean disconnect - down, but nothing went wrong.
            self.state.connected = False
            logger.info("disconnected cleanly")
        if self.on_update:
            self.on_update()

    def _handle_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        wall_time = time.time()
        try:
            text = msg.payload.decode("utf-8", errors="replace")
            payload = parse_line(text)
        except MalformedMessageError as exc:
            self.state.note_parse_error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive, must never crash the loop
            self.state.note_parse_error(f"unexpected error: {exc}")
        else:
            try:
                self.state.apply_payload(payload, wall_time)
            except Exception as exc:  # pragma: no cover - defensive
                self.state.note_parse_error(f"apply error: {exc}")
        if self.on_update:
            self.on_update()
