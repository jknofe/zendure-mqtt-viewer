"""MQTT I/O layer - it cannot command the device, by construction.

SAFETY: the write topic (`iot/.../properties/write`) drives a real battery
and inverter, and nothing here can reach it. publish()/will_set() raise
unconditionally, whatever the arguments - see tests/test_mqtt_guard.py.

The one message this tool can send is a request for a full report, which is
not a device command. It is allow-listed by *value*: the client holds one
exact (topic, payload) pair and send_allowed_request() refuses anything that
is not character-for-character that pair, so no edit can widen "refresh"
into "set output to 800 W" without defeating that check.
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


# The one message this tool may ever send: report every property you have.
# soh, maxTemp and softVersion only ever arrive in such a full report. A
# literal, not json.dumps(...), so the armed value cannot drift.
REFRESH_PAYLOAD = '{"properties": ["getAll"]}'

# The hub answers within a few seconds; anything faster is a held-down key.
REFRESH_MIN_INTERVAL = 10.0


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

    Subscribing is fine and is most of what this tool does. Publishing -
    including an empty payload, a retained-clear, or configuring a will
    message that the broker would publish on our behalf - is forbidden.

    ``allowed_request`` arms exactly one ``(topic, payload)`` pair, which
    ``send_allowed_request`` will accept and nothing else will. Left at None
    there is no way to publish anything at all.
    """

    def __init__(self, *args, allowed_request: Optional[tuple[str, str]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._allowed_request = allowed_request

    def publish(self, *args, **kwargs):  # type: ignore[override]
        raise PublishForbiddenError(
            "GuardedMqttClient.publish() was called. This tool never publishes "
            "device commands. If you meant the refresh request, use "
            "send_allowed_request()."
        )

    def will_set(self, *args, **kwargs):  # type: ignore[override]
        raise PublishForbiddenError(
            "GuardedMqttClient.will_set() was called. This tool must never "
            "configure a will message."
        )

    def send_allowed_request(self, topic: str, payload: str):
        """Publish the one pre-armed message, or refuse.

        Compares against the armed pair by value, so the caller cannot widen
        what is sendable - passing a different topic or a differently spelled
        payload is an error, not a new permission. Calls paho's publish
        directly because our own override exists to stop exactly this from
        being reachable by accident.
        """
        if self._allowed_request is None:
            raise PublishForbiddenError(
                "No request is armed on this client; refresh is disabled."
            )
        if (topic, payload) != self._allowed_request:
            raise PublishForbiddenError(
                f"Refused to publish to {topic!r}: only the armed refresh "
                f"request may be sent."
            )
        return mqtt.Client.publish(self, topic, payload, qos=0, retain=False)


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
        self._last_refresh: Optional[float] = None

        self._client = GuardedMqttClient(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
            allowed_request=(config.read_topic, REFRESH_PAYLOAD),
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

    def request_full_report(self, now: Optional[float] = None) -> bool:
        """Ask the hub to report everything. True if a request went out.

        Returns False rather than raising for every "not now" case: the
        caller is a keypress handler inside the draw loop, and a dashboard
        must not fall over because a key was pressed at an awkward moment.
        """
        now = time.time() if now is None else now
        if self._last_refresh is not None and now - self._last_refresh < REFRESH_MIN_INTERVAL:
            return False
        if not self.state.connected:
            logger.info("refresh requested while disconnected, ignored")
            return False
        try:
            self._client.send_allowed_request(self.config.read_topic, REFRESH_PAYLOAD)
        except (PublishForbiddenError, OSError, ValueError) as exc:
            logger.warning("refresh request failed: %s", exc)
            return False
        self._last_refresh = now
        self.state.note_refresh_requested(now)
        logger.info("requested full report on %s", self.config.read_topic)
        if self.on_update:
            self.on_update()
        return True

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
