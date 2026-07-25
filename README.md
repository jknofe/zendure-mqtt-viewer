# zendure-mqtt-viewer

A standalone, full-screen terminal dashboard for a Zendure SolarFlow battery
hub, fed by MQTT.

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

A full-screen, in-place-redrawing dashboard (curses) with four tabs you
switch between - it never scrolls, and only the active tab is drawn:

- **`[1] Overview`** - the at-a-glance view: a SoC bar gauge, a small
  Solar -> Hub -> Home power-flow diagram with a battery
  charge/discharge arrow, floor/target SoC.
- **`[2] Hub`** - two columns of compact label/value rows: hub *state*
  (SoC, pack state, bypass, hub/wifi/heat state, time remaining, WiFi
  SSID/MAC/IP) and hub *settings* (output/input limits, min SoC floor,
  target SoC, bypass mode, and the rest of the configuration fields).
- **`[3] Packs`** - a real table, one row per battery pack (keyed by
  serial number): SoC, state of health, max cell temp, max/min cell
  voltage, derived cell imbalance (`maxVol - minVol`, in mV), power,
  state, and how stale the row is.
- **`[4] Raw`** - the five undocumented `*Cycle` counters and any field
  the tool has never seen before, shown raw in a table. New firmware
  fields surface here instead of being silently dropped.

A status bar is pinned to the bottom row on every tab: connection state,
message count, parse error count, time since the last message, and key
hints. Switch tabs with `1`-`4` or `Tab`/`Shift-Tab`, quit with `q`.

### Why values dim instead of disappear, and show `--` instead of `0`

The hub's MQTT stream is **delta-only**: each message carries only the
fields that changed since the last one, and some fields (`minSoc`,
`passMode`, `socSet`, ...) may not appear for hours. This tool holds every
field's last known value in memory. A field that has never been reported
renders as `--` - never as `0`, because "never reported" and "reported as
zero" are different facts about the hub. A field whose last update is more
than 30s old is shown dimmed with a short age marker (`2s`, `5m`, `1h`)
instead of eating a whole line per field - the staleness information is
still there, just compact.

## Setup

```sh
git clone <this repo>
cd zendure-mqtt-viewer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.11+ (developed against 3.14) with a terminal that
supports `curses` (standard on macOS/Linux terminals). The only runtime
dependency is `paho-mqtt`; everything else is the standard library.

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
# full-screen curses dashboard, tabs 1-4, q to quit, resize-aware
.venv/bin/python -m zendure_mqtt_viewer

# print one plain-text frame and exit - no curses, good for demos/scripts
.venv/bin/python -m zendure_mqtt_viewer --once --tab overview

# run for 60 seconds then exit (curses on a tty; headless + one final
# plain-text frame when piped, never a growing stream of frames)
.venv/bin/python -m zendure_mqtt_viewer --duration 60

# replay a capture file offline, no network at all - drives the exact
# same dashboard
.venv/bin/python -m zendure_mqtt_viewer --replay samples/sample_capture_1.jsonl --once --tab packs

# replay paced at real-time speed instead of as fast as possible
.venv/bin/python -m zendure_mqtt_viewer --replay samples/sample_capture_2.jsonl --replay-speed 1.0
```

Other flags: `--config PATH`, `--interval SECONDS` (screen refresh rate,
default 1.0), `--tab {overview,hub,packs,raw}` (initial/only tab),
`--width N` / `--height N` (override detected terminal size - only affects
the plain-text `--once`/piped path; the interactive dashboard always reads
the live terminal size and reacts to resize).

On a terminal too small to lay out (below roughly 54x14), the dashboard
shows a compact "terminal too small" message instead of crashing or
wrapping into a mess.

Ctrl-C at any point stops cleanly, restores the terminal, and (in live
mode) disconnects from the broker. `curses.wrapper()` guarantees terminal
restoration even on an unexpected exception.

## Running tests

```sh
.venv/bin/pytest
```

Tests run fully offline, none touch curses or a real terminal - the
dashboard layout is pure Python (`layout.py`) that builds a fixed-size grid
of styled text, so it's tested by rendering into WxH buffers and asserting
on them directly. Coverage includes: every scaling conversion, every enum
decode, the `59940` remaining-time sentinel, delta-merge (a message with
one field must not clear previously-seen fields), never-seen-vs-zero,
malformed-line handling (using the real captures in `samples/`, each of
which contains one deliberately malformed line), unknown-field capture (top
level, `properties`, and `packData`), config loading/env overrides, the
publish guard, and the dashboard layout itself (frames never exceed the
requested rows/cols at several sizes, exactly fill the screen when there's
room, tab switching changes content, the active tab is marked, the small-
terminal fallback doesn't crash).

## Architecture

- `decode.py` / `state.py` - pure decoding and delta-merge state, unchanged
  by the dashboard rework, no I/O.
- `layout.py` - pure dashboard layout: builds a `Frame` (a fixed rows x cols
  grid of styled text spans) from a `DashboardState` snapshot for the
  active tab. No curses, no I/O - this is what makes it unit-testable and
  what lets `--replay`/`--once` print the exact same thing curses draws.
- `tui.py` - thin curses runtime: blits a `Frame` onto the real terminal
  and turns keypresses into tab changes. Everything layout-related lives in
  `layout.py`; this file only calls `addstr()`.
- `mqtt_client.py` - the publish-guarded subscriber.
- `replay.py` - feeds a capture file through the same `DashboardState`
  the live subscriber uses.
- `cli.py` - argument parsing and mode dispatch (interactive curses vs.
  `--once`/`--duration` plain-text snapshot).

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
