"""Config loading tests. Uses only a throwaway temp TOML file - never touches
the real ~/.config/zendure-mqtt-viewer/config.toml and never asserts on its
contents.
"""
from __future__ import annotations


import pytest

from zendure_mqtt_viewer import config as config_mod


def test_missing_config_file_and_no_env_raises_clear_error(tmp_path, monkeypatch):
    for var in (
        config_mod.ENV_HOST,
        config_mod.ENV_USERNAME,
        config_mod.ENV_PASSWORD,
        config_mod.ENV_PORT,
        config_mod.ENV_CONFIG_PATH,
    ):
        monkeypatch.delenv(var, raising=False)

    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(config_mod.ConfigError) as exc_info:
        config_mod.load_broker_config(str(missing))
    assert str(missing) in str(exc_info.value)


def test_loads_from_toml_file(tmp_path, monkeypatch):
    for var in (config_mod.ENV_HOST, config_mod.ENV_USERNAME, config_mod.ENV_PASSWORD, config_mod.ENV_PORT):
        monkeypatch.delenv(var, raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('host = "10.0.0.99"\nport = 1884\nusername = "u"\npassword = "p"\n')

    cfg = config_mod.load_broker_config(str(cfg_file))
    assert cfg.host == "10.0.0.99"
    assert cfg.port == 1884
    assert cfg.username == "u"
    assert cfg.report_topic == "/73bkTV/AB1234CD/properties/report"


def test_env_vars_override_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('host = "file-host"\nusername = "file-user"\npassword = "file-pass"\n')
    monkeypatch.setenv(config_mod.ENV_HOST, "env-host")

    cfg = config_mod.load_broker_config(str(cfg_file))
    assert cfg.host == "env-host"
    assert cfg.username == "file-user"  # not overridden, still from file


def test_env_vars_alone_are_sufficient_without_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv(config_mod.ENV_HOST, "env-host")
    monkeypatch.setenv(config_mod.ENV_USERNAME, "env-user")
    monkeypatch.setenv(config_mod.ENV_PASSWORD, "env-pass")
    missing = tmp_path / "does-not-exist.toml"

    cfg = config_mod.load_broker_config(str(missing))
    assert cfg.host == "env-host"
