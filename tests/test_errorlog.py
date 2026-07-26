"""Errors go to a file, and never to the terminal.

The rule being pinned: while the dashboard is up, the terminal belongs to
curses. A single log line on stderr paints over the frame and stays there,
which is what "unspecified error" did. So there are two halves to test -
that the log file actually receives the detail, and that nothing at all
escapes to stdout/stderr, including on every failure path of the logging
setup itself.
"""
from __future__ import annotations

import logging
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.reasoncodes import ReasonCode

from zendure_mqtt_viewer import errorlog
from zendure_mqtt_viewer.config import BrokerConfig
from zendure_mqtt_viewer.mqtt_client import Subscriber
from zendure_mqtt_viewer.state import DashboardState

CONNACK = mqtt.CONNACK >> 4
UNSPECIFIED_ERROR = 0x80


@pytest.fixture(autouse=True)
def _restore_logging():
    """Leave the package logger exactly as found - other tests share it."""
    logger = logging.getLogger(errorlog.PACKAGE_LOGGER)
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    raising = logging.raiseExceptions
    yield
    errorlog.configure(None)
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate
    logging.raiseExceptions = raising


# ---------------------------------------------------------------------------
# The file gets the detail
# ---------------------------------------------------------------------------


def test_errors_are_written_to_the_log_file(tmp_path):
    path = tmp_path / "error.log"
    assert errorlog.configure(path) == path

    logging.getLogger("zendure_mqtt_viewer.mqtt_client").error("connect refused: %s", "Unspecified error")

    text = path.read_text(encoding="utf-8")
    assert "connect refused: Unspecified error" in text
    assert "ERROR" in text
    assert "zendure_mqtt_viewer.mqtt_client" in text


def test_a_refused_connect_lands_in_the_log(tmp_path):
    path = tmp_path / "error.log"
    errorlog.configure(path)

    state = DashboardState()
    cfg = BrokerConfig(host="broker.invalid", port=1883, username=None, password=None)
    sub = Subscriber(cfg, state)

    class _Client:
        def subscribe(self, topic, qos=0):
            raise AssertionError("must not subscribe after a refusal")

    sub._handle_connect(_Client(), None, {}, ReasonCode(CONNACK, identifier=UNSPECIFIED_ERROR))

    assert "Unspecified error" in path.read_text(encoding="utf-8")


def test_a_malformed_message_lands_in_the_log(tmp_path):
    path = tmp_path / "error.log"
    errorlog.configure(path)

    DashboardState().note_parse_error("invalid JSON: Expecting value: line 1 column 1")

    assert "invalid JSON" in path.read_text(encoding="utf-8")


def test_the_file_is_not_created_until_something_goes_wrong(tmp_path):
    path = tmp_path / "error.log"
    errorlog.configure(path)
    assert not path.exists()


def test_the_log_directory_is_created_on_demand(tmp_path):
    path = tmp_path / "nested" / "deeper" / "error.log"
    assert errorlog.configure(path) == path
    logging.getLogger("zendure_mqtt_viewer.test").error("boom")
    assert path.exists()


def test_info_level_noise_is_not_logged_by_default(tmp_path):
    path = tmp_path / "error.log"
    errorlog.configure(path)
    logging.getLogger("zendure_mqtt_viewer.test").info("connected, subscribed to ...")
    assert not path.exists()


# ---------------------------------------------------------------------------
# Nothing escapes to the terminal
# ---------------------------------------------------------------------------


def test_nothing_is_written_to_stdout_or_stderr(tmp_path, capsys):
    errorlog.configure(tmp_path / "error.log")
    logging.getLogger("zendure_mqtt_viewer.mqtt_client").error("connect refused: Unspecified error")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_records_do_not_propagate_to_the_root_logger(tmp_path):
    # A root handler installed by anything else must not become a second,
    # terminal-bound destination for our records.
    errorlog.configure(tmp_path / "error.log")
    assert logging.getLogger(errorlog.PACKAGE_LOGGER).propagate is False


def test_an_unconfigured_package_logger_still_stays_quiet(capsys):
    # logging.lastResort would otherwise print WARNING+ to stderr. The
    # NullHandler in __init__ is what prevents that before configure() runs.
    logger = logging.getLogger(errorlog.PACKAGE_LOGGER)
    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)

    logging.getLogger("zendure_mqtt_viewer.state").warning("parse error: invalid JSON")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_an_unwritable_log_location_is_survivable(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    path = blocker / "error.log"  # parent is a file: mkdir must fail

    assert errorlog.configure(path) is None
    assert errorlog.active_path() is None

    logging.getLogger("zendure_mqtt_viewer.test").error("boom")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_logging_nowhere_is_a_supported_choice(capsys):
    assert errorlog.configure(None) is None
    assert errorlog.active_path() is None
    logging.getLogger("zendure_mqtt_viewer.test").error("boom")
    assert capsys.readouterr().err == ""


def test_reconfiguring_does_not_duplicate_records(tmp_path):
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    errorlog.configure(first)
    errorlog.configure(second)

    logging.getLogger("zendure_mqtt_viewer.test").error("only once")

    assert not first.exists()
    assert second.read_text(encoding="utf-8").count("only once") == 1


# ---------------------------------------------------------------------------
# Where the file goes
# ---------------------------------------------------------------------------


def test_explicit_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(errorlog.ENV_LOG_PATH, str(tmp_path / "from-env.log"))
    assert errorlog.resolve_log_path(str(tmp_path / "from-cli.log")) == tmp_path / "from-cli.log"


def test_env_var_overrides_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(errorlog.ENV_LOG_PATH, str(tmp_path / "from-env.log"))
    assert errorlog.resolve_log_path() == tmp_path / "from-env.log"


def test_default_sits_next_to_the_cache(tmp_path, monkeypatch):
    monkeypatch.delenv(errorlog.ENV_LOG_PATH, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert errorlog.resolve_log_path() == tmp_path / "zendure-mqtt-viewer" / "error.log"


def test_home_relative_paths_are_expanded(monkeypatch):
    monkeypatch.setenv(errorlog.ENV_LOG_PATH, "~/somewhere/error.log")
    assert errorlog.resolve_log_path() == Path.home() / "somewhere" / "error.log"
