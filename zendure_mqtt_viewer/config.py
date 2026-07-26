"""Broker configuration loading.

Credentials live in a TOML file (mode 600) with keys host/port/username/
password. This module reads that file at runtime with tomllib. It never
logs, prints, or returns the raw file content - callers get a BrokerConfig
dataclass and are expected to keep username/password out of any output.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (Debian 11 ships 3.9)
    # tomli is the same parser tomllib was adopted from, with the same API,
    # so everything below is unchanged. Declared in requirements.txt with a
    # python_version marker, so modern interpreters never install it.
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zendure-mqtt-viewer" / "config.toml"

ENV_CONFIG_PATH = "ZENDURE_MQTT_CONFIG"
ENV_HOST = "ZENDURE_MQTT_HOST"
ENV_PORT = "ZENDURE_MQTT_PORT"
ENV_USERNAME = "ZENDURE_MQTT_USERNAME"
ENV_PASSWORD = "ZENDURE_MQTT_PASSWORD"
ENV_PRODUCT_ID = "ZENDURE_MQTT_PRODUCT_ID"
ENV_DEVICE_ID = "ZENDURE_MQTT_DEVICE_ID"

DEFAULT_PORT = 1883
DEFAULT_PRODUCT_ID = "73bkTV"
DEFAULT_DEVICE_ID = "AB1234CD"


class ConfigError(RuntimeError):
    """Raised when broker configuration cannot be resolved."""


@dataclasses.dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    product_id: str = DEFAULT_PRODUCT_ID
    device_id: str = DEFAULT_DEVICE_ID

    @property
    def report_topic(self) -> str:
        # Leading slash is deliberate - see PROTOCOL.md.
        return f"/{self.product_id}/{self.device_id}/properties/report"


def resolve_config_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path).expanduser()
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid TOML: {exc}") from exc


def load_broker_config(cli_config_path: Optional[str] = None) -> BrokerConfig:
    path = resolve_config_path(cli_config_path)

    have_full_env = all(os.environ.get(k) for k in (ENV_HOST, ENV_USERNAME, ENV_PASSWORD))

    file_data: dict = {}
    if path.exists():
        file_data = _read_toml(path)
    elif not have_full_env:
        raise ConfigError(
            f"Config file not found: {path}\n\n"
            "Create it with mode 600 containing:\n"
            '  host = "broker-hostname-or-ip"\n'
            "  port = 1883\n"
            '  username = "..."\n'
            '  password = "..."\n\n'
            f"Or set {ENV_HOST}, {ENV_USERNAME}, {ENV_PASSWORD} (and optionally "
            f"{ENV_PORT}) as environment variables, or pass --config <path>."
        )

    host = os.environ.get(ENV_HOST) or file_data.get("host")
    port_raw = os.environ.get(ENV_PORT) or file_data.get("port") or DEFAULT_PORT
    username = os.environ.get(ENV_USERNAME) or file_data.get("username")
    password = os.environ.get(ENV_PASSWORD) or file_data.get("password")
    product_id = os.environ.get(ENV_PRODUCT_ID) or file_data.get("product_id") or DEFAULT_PRODUCT_ID
    device_id = os.environ.get(ENV_DEVICE_ID) or file_data.get("device_id") or DEFAULT_DEVICE_ID

    if not host:
        raise ConfigError(
            f"Missing 'host' - set it in {path} or via {ENV_HOST}."
        )

    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid port value {port_raw!r}") from exc

    return BrokerConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        product_id=str(product_id),
        device_id=str(device_id),
    )
