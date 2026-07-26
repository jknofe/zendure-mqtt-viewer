# zendure-mqtt-viewer

A standalone, full-screen terminal dashboard for a Zendure SolarFlow battery
hub, fed by MQTT.

## This tool cannot command your hardware

It subscribes to the hub's `.../properties/report` topic. **It never sends a
device command, under any circumstance.** The write topic
(`iot/.../properties/write`) drives a real battery and inverter - this tool
cannot reach it even by accident:

- `zendure_mqtt_viewer/mqtt_client.py` defines `GuardedMqttClient`, a
  subclass of `paho.mqtt.client.Client` whose `publish()` and `will_set()`
  are overridden to unconditionally raise `PublishForbiddenError` (a
  `RuntimeError`), regardless of arguments.
- `tests/test_mqtt_guard.py` asserts this for several call shapes, including
  an empty payload and the exact shape of a real write command.
- `tests/test_refresh.py` asserts that the write topic is refused even
  through the one path that is allowed to publish (below).

### The single exception: `--allow-refresh`

Some fields - `soh` (battery state of health), `maxTemp`, `softVersion` -
are **never** sent in the normal delta stream. They only appear in a full
report, which the hub sends when asked. Measured here: none of the three
arrived in 30 minutes of passive listening; after one request, all three
arrived in 1.9 seconds.

Asking means publishing, so it is off by default and deliberately narrow.
With `--allow-refresh`, the `r` key sends exactly this and nothing else:

```
topic:   iot/<product_id>/<device_id>/properties/read
payload: {"properties": ["getAll"]}
```

That is a request for data, not a device command. It is allow-listed **by
value**: `GuardedMqttClient` is handed that one `(topic, payload)` pair at
construction and refuses anything that is not character-for-character
identical, `publish()` still raises for every caller, and without the flag
nothing is armed at all. So a future edit cannot widen "refresh" into "set
the output limit" - it would have to defeat the value check to do it.
Requests are rate-limited to one per 10 seconds and ignored while
disconnected.

Note the topic asymmetry: reports arrive on `/<product_id>/...` with a
leading slash and no prefix, while requests go to `iot/<product_id>/...`.
The request topic is not derived from the report topic for that reason;
publishing to the wrong one fails silently.

## What it shows

A full-screen, in-place-redrawing dashboard (curses) with four tabs you
switch between - it never scrolls, and only the active tab is drawn:

- **`[1] Overview`** - the at-a-glance view: a SoC bar gauge, a small
  Solar / Hub / Home power-flow diagram with a battery charge/discharge
  arrow, floor/target SoC. The watts are written inside the arrows
  (`── 276 W ──▶`), so how much is moving and which way read as one
  thing. The battery arrow points down into the pack when charging and up
  into the hub when discharging. The arrows compact on a small terminal
  (fewer shaft segments) rather than dropping the numbers.
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
hints. Switch tabs with `1`-`4` or `Tab`/`Shift-Tab`, quit with `q`. With
`--allow-refresh`, `r` asks the hub for a full report (see above) and the
hint area briefly reads `refresh sent`.

Colour carries meaning rather than decoration: the SoC gauge runs red
below 20% / yellow below 50% / green above, charging is green and
discharging yellow, solar input is yellow and house output magenta, the
connection state in the title bar is green when connected and red when
not, and chrome (borders, labels, key hints) is dimmed so the numbers
stand out. Terminals without colour fall back to bold/dim/reverse and
stay fully readable.

### Errors go to a log file, never to the screen

The dashboard never prints error text. Nothing that goes wrong is allowed
to draw a message, a reason string, or a stack trace into the frame:
under curses, anything written to the terminal outside the layout lands on
top of the dashboard and stays there.

Everything is written to `~/.cache/zendure-mqtt-viewer/error.log` instead,
timestamped and with more detail than a status bar could ever hold. The
file is created only when there is something to record, and rotates at
512 KB (two backups) so a hub that reconnects all night cannot fill the
disk. `--error-log PATH` puts it elsewhere, `--no-error-log` turns it off,
and `ZENDURE_MQTT_VIEWER_LOG` sets the path from the environment. If any
error was recorded, a single line naming the log file is printed on exit,
after the terminal has been handed back.

What the dashboard does show is *state*, in cells it already owns: the
title bar reads `CONNECTED` in green or `DISCONNECTED` in red, and the
status bar carries the parse error count. So a refused connect (for
example MQTT reason code `0x80`, "Unspecified error") looks like a
`DISCONNECTED` title and an unchanged layout, with the broker's own words
waiting in the log. The tool still never claims to be connected while it
is subscribed to nothing.

### Values survive a restart

Because the stream is delta-only (see below), rare fields like
`packState`, `minSoc`, `socSet` and the firmware versions can take a long
time to be re-broadcast, so a fresh start used to show `--` for them for
minutes. The last known values are cached to
`~/.cache/zendure-mqtt-viewer/last-seen.json` while running and on exit,
and reloaded at startup. Restored values keep their original timestamps,
so they appear dimmed with their true age rather than posing as live
readings, and the first live message for a field replaces it. Only raw
values are cached, never formatted text, so decoding changes apply to
restored values too. Use `--no-cache` to start empty, or `--cache PATH`
to put the file elsewhere. A missing, corrupt, or stale (>24h) cache is
ignored silently: it can never stop the dashboard from starting.

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

Requires Python 3.9+ (developed against 3.14) with a terminal that
supports `curses` (standard on macOS/Linux terminals). The only runtime
dependency is `paho-mqtt`, plus `tomli` on Python older than 3.11, where
`tomllib` is not yet in the standard library. That covers Debian 11 and
Raspberry Pi OS bullseye, which still ship 3.9.

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
the live terminal size and reacts to resize), `--cache PATH` and
`--no-cache` (the last-seen value cache described above; live mode only,
`--replay` never reads or writes it), `--error-log PATH` and
`--no-error-log` (the error log described above).

On a terminal too small to lay out (below roughly 54x14), the dashboard
shows a compact "terminal too small" message instead of crashing or
wrapping into a mess.

Ctrl-C at any point stops cleanly, restores the terminal, and (in live
mode) disconnects from the broker. `curses.wrapper()` guarantees terminal
restoration even on an unexpected exception.

### Running it on a headless box

`contrib/zendure-mqtt-viewer.service` is a systemd **user** unit that keeps
the dashboard in a tmux session across logout and reboot, still attachable:

```sh
mkdir -p ~/.config/systemd/user ~/.local/bin
cp contrib/zendure-mqtt-viewer.service ~/.config/systemd/user/
cp contrib/zendure ~/.local/bin/ && chmod +x ~/.local/bin/zendure
systemctl --user daemon-reload
systemctl --user enable --now zendure-mqtt-viewer
loginctl enable-linger "$USER"    # start at boot without logging in

zendure                           # attach; Ctrl-b d detaches, it keeps running
```

The session lives on its own tmux socket (`-L zendure`), so stopping the
unit can never take down tmux sessions you started yourself.

**That socket is also why `tmux ls` and `tmux attach` will not show it** -
they only ever look at the default socket, so the dashboard appears to be
gone when it is running perfectly well. Use the `zendure` wrapper from
`contrib/` (it also takes `status`, `log` and `restart`), or the long form
`tmux -L zendure attach -t zendure`.

Restarting costs nothing: the last-seen cache means the dashboard comes
back with its values already populated.

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
publish guard, error routing (records reach the log file, nothing reaches
stdout or stderr, no error string is ever drawn into a frame, and an
unwritable log location is survivable), and the dashboard layout itself
(frames never exceed the requested rows/cols at several sizes, exactly
fill the screen when there's room, tab switching changes content, the
active tab is marked, the small-terminal fallback doesn't crash).

## Architecture

- `decode.py` / `state.py` - pure decoding and delta-merge state, unchanged
  by the dashboard rework, no I/O.
- `layout.py` - pure dashboard layout: builds a `Frame` (a fixed rows x cols
  grid of styled text spans) from a `DashboardState` snapshot for the
  active tab. No curses, no I/O - this is what makes it unit-testable and
  what lets `--replay`/`--once` print the exact same thing curses draws.
- `tui.py` - thin curses runtime: blits a `Frame` onto the real terminal
  and turns keypresses into tab changes. Everything layout-related lives in
  `layout.py`; this file only calls `addstr()` and resolves span attributes
  ("bold ok", "muted", ...) into curses attributes and colour pairs.
- `persist.py` - the last-seen value cache: atomic save, tolerant load.
- `errorlog.py` - the one place errors are allowed to go. Installs a
  rotating file handler on the package logger with `propagate = False`, so
  no log record can reach a root handler and be printed over the running
  dashboard. A `NullHandler` in `__init__.py` covers the window before it
  is configured, where `logging.lastResort` would otherwise write to
  stderr.
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

The identifying fields are anonymised: the WiFi SSID, the MAC address, the
LAN IP, the hub serial (`AB1234CD`) and the pack serial (`ZZ0EXAMPLE00001`)
are placeholders. Every measurement is untouched real data.

## Notes on the protocol doc

While decoding real capture data, one field's documented scaling didn't
match what the hub actually sent: `soh` (pack state of health) is
documented as a direct percent, but the only observed value (`978`) only
makes sense as `97.8%` (i.e. scaled by /10, like `minSoc`/`socSet`). This
tool decodes `soh` as /10; see the comment next to `PACK_FIELD_SPECS["soh"]`
in `zendure_mqtt_viewer/decode.py`.

## License

MIT, see [LICENSE](LICENSE).
