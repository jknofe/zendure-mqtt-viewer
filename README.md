# zendure-mqtt-viewer

A standalone, terminal ASCII dashboard for a Zendure SolarFlow battery hub,
fed by MQTT.

## This tool is strictly read-only

It subscribes to the hub's `.../properties/report` topic and nothing else.
**It never publishes to the MQTT broker, under any circumstance.** The write
topic (`iot/.../properties/write`) commands a real battery and inverter -
this tool cannot reach it even by accident:

- `zendure_mqtt_viewer/mqtt_client.py` defines `GuardedMqttClient`, a
  subclass of `paho.mqtt.client.Client` whose `publish()` and `will_set()`
  are overridden to unconditionally raise `PublishForbiddenError` (a
  `RuntimeError`), regardless of arguments.
- `tests/test_mqtt_guard.py` asserts this for several call shapes, including
  an empty payload and the exact shape of a real write command.
- `grep -rn "publish" --include='*.py' --exclude-dir=.venv .` shows no
  live/callable publish anywhere in this codebase except that guard and its
  test.

## What it shows

A live-refreshing text dashboard, grouped into:

- **Hub Live Power** - solar in (total + per string), battery charge/discharge
  power, output to home, grid/smart power.
- **Hub State** - state of charge, pack state, bypass, hub/wifi/heat state,
  pack count, discharge/charge time remaining, plus WiFi SSID/MAC/IP when
  the hub has reported them.
- **Hub Settings** - output/input limits, inverter max power, min SoC floor,
  target SoC, bypass mode, and the other configuration fields.
- **Battery Packs** - one block per pack (keyed by serial number), with SoC,
  state of health, max cell temperature, max/min cell voltage, pack
  voltage, power, state, firmware version, and derived cell imbalance
  (`maxVol - minVol`, shown in mV).
- **Undecoded / Unknown Fields** - the five `*Cycle` counters (no
  documentation exists for them) and any property the tool has never seen
  before, shown raw. New firmware fields show up here instead of being
  silently dropped.
- **Connection Status** - connected/disconnected (or replay-file mode),
  messages received, parse error count, time since the last message.

### Why everything shows an age, and `--` instead of `0`

The hub's MQTT stream is **delta-only**: each message carries only the
fields that changed since the last one, and some fields (`minSoc`,
`passMode`, `socSet`, ...) may not appear for hours. This tool holds every
field's last known value in memory and shows how long ago it was last
updated. A field that has never been reported is shown as `--` with age
`never seen` - it is never rendered as `0`, because "never reported" and
"reported as zero" are different facts about the hub.

## Setup

```sh
git clone <this repo>
cd zendure-mqtt-viewer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.11+ (developed against 3.14). The only runtime dependency
is `paho-mqtt`; everything else is the standard library.

### Config file

Credentials are read from a TOML file, by default
`~/.config/zendure-mqtt-viewer/config.toml`. It must exist with mode `600`
and contain (keys only - see below for why no values are shown here):

```toml
host = "..."
port = ...
username = "..."
password = "..."
```

Optional keys: `product_id` (default `73bkTV`), `device_id` (default
`AB1234CD`) - these plus the report topic path are what build the MQTT
subscription topic, in case a second hub is added later.

**Values are never printed, logged, committed, or otherwise surfaced by
this tool or its tests.** `--config <path>` overrides the file location.
Every field can also be set via environment variable, which takes priority
over the file:

| Env var | Overrides |
|---|---|
| `ZENDURE_MQTT_CONFIG` | config file path |
| `ZENDURE_MQTT_HOST` | `host` |
| `ZENDURE_MQTT_PORT` | `port` |
| `ZENDURE_MQTT_USERNAME` | `username` |
| `ZENDURE_MQTT_PASSWORD` | `password` |
| `ZENDURE_MQTT_PRODUCT_ID` | `product_id` |
| `ZENDURE_MQTT_DEVICE_ID` | `device_id` |

If the config file is missing and the required environment variables aren't
set either, the tool exits with a clear message explaining what to create
or set.

## Usage

```sh
# live dashboard, refreshes in place, Ctrl-C to quit
.venv/bin/python -m zendure_mqtt_viewer

# print one frame and exit (waits briefly for a first message)
.venv/bin/python -m zendure_mqtt_viewer --once

# run for 60 seconds then print the final frame and exit
.venv/bin/python -m zendure_mqtt_viewer --duration 60

# replay a capture file offline, no network at all
.venv/bin/python -m zendure_mqtt_viewer --replay samples/sample_capture_1.jsonl --once

# replay paced at real-time speed instead of as fast as possible
.venv/bin/python -m zendure_mqtt_viewer --replay samples/sample_capture_2.jsonl --replay-speed 1.0
```

Other flags: `--config PATH`, `--interval SECONDS` (screen refresh rate,
default 1.0), `--width N` (override detected terminal width).

When stdout isn't a terminal (e.g. piped to a file), the tool prints
successive frames separated by a divider instead of doing in-place ANSI
redraw, so `--duration 60 | tee out.txt` works for unattended capture.
Ctrl-C at any point stops cleanly, restores the cursor, and (in live mode)
disconnects from the broker.

## Running tests

```sh
.venv/bin/pytest
```

Tests run fully offline. They cover: every scaling conversion, every enum
decode, the `59940` remaining-time sentinel, delta-merge (a message with
one field must not clear previously-seen fields), never-seen-vs-zero,
malformed-line handling (using the real captures in `samples/`, each of
which contains one deliberately malformed line), unknown-field capture (top
level, `properties`, and `packData`), config loading/env overrides, and the
publish guard.

## Sample data

`samples/sample_capture_1.jsonl` and `samples/sample_capture_2.jsonl` are
real captures of the report topic, used by `--replay` and by the test
suite. Each contains one malformed line, matching the ~1-in-600 malformed
message rate observed on real traffic.

## Notes on the protocol doc

While decoding real capture data, one field's documented scaling didn't
match what the hub actually sent: `soh` (pack state of health) is
documented as a direct percent, but the only observed value (`978`) only
makes sense as `97.8%` (i.e. scaled by /10, like `minSoc`/`socSet`). This
tool decodes `soh` as /10; see the comment next to `PACK_FIELD_SPECS["soh"]`
in `zendure_mqtt_viewer/decode.py`.
