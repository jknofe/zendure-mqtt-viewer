"""Command-line entry point.

Wires together config loading, the MQTT subscriber (or --replay file
reader), the DashboardState, and the dashboard presentation layer
(layout.py for the pure frame, tui.py for the curses runtime).

Modes:
  - default (tty, no --once/--duration): full curses dashboard, tab
    switching with 1-4/Tab/Shift-Tab, quit with q, resize-aware.
  - --once: build state, print exactly one plain-text frame, exit. No
    curses. Used for demos/verification and non-interactive scripting.
  - --duration N: on a tty, run the curses dashboard for N seconds then
    exit; off a tty (piped), wait N seconds headless then print exactly
    one final plain-text frame - never a growing stream of frames.
  - --replay FILE: loads the capture file fully (no network), then follows
    the same mode rules above against the resulting static state.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import errorlog
from . import layout
from . import persist
from . import replay as replay_mod
from .mqtt_client import Subscriber
from .state import DashboardState

logger = logging.getLogger(__name__)

DEFAULT_TAB = "overview"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zendure-mqtt-viewer",
        description=(
            "Live dashboard for a Zendure SolarFlow hub over MQTT. This tool "
            "never sends a device command; with --allow-refresh it can send "
            "one request for a full report, and nothing else."
        ),
    )
    p.add_argument("--config", metavar="PATH", help="path to config.toml (default: ~/.config/zendure-mqtt-viewer/config.toml)")
    p.add_argument("--replay", metavar="FILE.jsonl", help="replay a capture file instead of connecting to a broker")
    p.add_argument("--replay-speed", type=float, default=0.0, help="pace replay using captured timestamps (0 = as fast as possible, 1.0 = real time)")
    p.add_argument("--once", action="store_true", help="print a single plain-text frame and exit (no curses)")
    p.add_argument("--duration", type=float, metavar="SECONDS", help="run for this many seconds then exit")
    p.add_argument("--interval", type=float, default=1.0, help="screen refresh interval in seconds (default 1.0)")
    p.add_argument("--width", type=int, help="override detected terminal width (plain-text modes only)")
    p.add_argument("--height", type=int, help="override detected terminal height (plain-text modes only)")
    p.add_argument("--cache", metavar="PATH", help="path to the last-seen value cache (default: ~/.cache/zendure-mqtt-viewer/last-seen.json)")
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="start from an empty dashboard and do not save last-seen values",
    )
    p.add_argument(
        "--allow-refresh",
        action="store_true",
        help=(
            "enable the [r] key, which asks the hub to report every property "
            "(soh and friends only ever arrive in such a full report). This is "
            "the only message this tool can send, and it is never a device "
            "command; without this flag it cannot publish at all"
        ),
    )
    p.add_argument(
        "--error-log",
        metavar="PATH",
        help="where to write the error log (default: ~/.cache/zendure-mqtt-viewer/error.log)",
    )
    p.add_argument(
        "--no-error-log",
        action="store_true",
        help="do not write an error log (errors are still never shown on screen)",
    )
    p.add_argument(
        "--tab",
        choices=[t.lower() for t in layout.TABS],
        default=DEFAULT_TAB,
        help="initial/only tab to show (default: overview)",
    )
    return p


def _terminal_size(args: argparse.Namespace) -> tuple[int, int]:
    fallback = shutil.get_terminal_size(fallback=(100, 30))
    cols = args.width or fallback.columns
    rows = args.height or fallback.lines
    return cols, rows


def _print_once(state: DashboardState, mode: str, args: argparse.Namespace, device_id: str = "") -> None:
    cols, rows = _terminal_size(args)
    frame = layout.build_frame(state, args.tab, cols, rows, time.time(), mode=mode, device_id=device_id)
    sys.stdout.write(frame.to_text())
    sys.stdout.write("\n")
    sys.stdout.flush()


def _run_interactive(
    state: DashboardState,
    mode: str,
    args: argparse.Namespace,
    device_id: str = "",
    on_persist=None,
    on_refresh=None,
) -> None:
    # Imported lazily so --once/plain-text paths never need a real tty or
    # curses terminfo (handy for CI / piping / tests).
    import curses

    from . import tui

    curses.wrapper(
        tui.run,
        state,
        mode=mode,
        device_id=device_id,
        interval=args.interval,
        duration=args.duration,
        initial_tab=args.tab,
        on_persist=on_persist,
        on_refresh=on_refresh,
    )


def _run_headless_for_duration(state: DashboardState, args: argparse.Namespace) -> None:
    deadline = time.time() + (args.duration or 0)
    while time.time() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.time())))


def _dispatch(
    state: DashboardState,
    mode: str,
    args: argparse.Namespace,
    device_id: str = "",
    on_persist=None,
    on_refresh=None,
) -> None:
    tty = sys.stdout.isatty()

    if args.once:
        if mode == "live":
            deadline = time.time() + min(5.0, args.duration or 5.0)
            while state.messages_received == 0 and time.time() < deadline:
                time.sleep(0.1)
        _print_once(state, mode, args, device_id)
        return

    if tty:
        _run_interactive(state, mode, args, device_id, on_persist=on_persist, on_refresh=on_refresh)
        return

    # Piped / non-interactive: no curses. Wait out --duration (if any) then
    # print exactly one final frame - never a growing stream of blocks.
    if args.duration is not None:
        _run_headless_for_duration(state, args)
    else:
        # No duration given and not a tty: still just emit one snapshot -
        # there is nowhere sensible to "watch" a redraw on a pipe.
        if mode == "live":
            time.sleep(min(2.0, args.interval))
    _print_once(state, mode, args, device_id)


def _note_error_log(state: DashboardState) -> None:
    """One stderr line pointing at the log, printed after the dashboard is gone.

    The dashboard itself never says a word about errors, so without this the
    log would be a file nobody knows to look at. It is printed once, on the
    way out, when the terminal already belongs to the shell again.
    """
    path = errorlog.active_path()
    if path is None or (not state.parse_errors and not state.connection_error):
        return
    print(f"Errors occurred during this run, see {path}", file=sys.stderr)


def _run_replay(args: argparse.Namespace) -> int:
    path = Path(args.replay).expanduser()
    if not path.exists():
        print(f"Replay file not found: {path}", file=sys.stderr)
        return 2

    state = DashboardState()
    replay_mod.replay_file(path, state, speed=args.replay_speed)
    state.connected = True  # informational only; layout overrides wording in replay mode

    _dispatch(state, "replay", args)
    return 0


def _run_live(args: argparse.Namespace) -> int:
    try:
        cfg = config_mod.load_broker_config(args.config)
    except config_mod.ConfigError as exc:
        # Fatal and pre-curses: stderr is the right place for this one, and
        # there is no dashboard yet for it to disturb.
        logger.error("configuration error: %s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    state = DashboardState()

    # Seed from the last run before connecting, so rare fields (packState,
    # minSoc, socSet, ...) are on screen from the first frame instead of
    # blank until the hub next happens to broadcast them.
    cache_path = None if args.no_cache else persist.resolve_cache_path(args.cache)
    on_persist = None
    if cache_path is not None:
        persist.load(state, cache_path)
        on_persist = lambda: persist.save(state, cache_path)  # noqa: E731

    subscriber = Subscriber(cfg, state, allow_refresh=args.allow_refresh)
    on_refresh = subscriber.request_full_report if args.allow_refresh else None
    try:
        subscriber.start()
    except OSError as exc:
        # Connection refused / DNS failure / no route: report it rather than
        # dying with a traceback over a half-drawn screen.
        logger.error("cannot reach broker %s:%s: %s", cfg.host, cfg.port, exc)
        print(f"Cannot reach broker {cfg.host}:{cfg.port}: {exc}", file=sys.stderr)
        return 2

    try:
        _dispatch(
            state,
            "live",
            args,
            device_id=cfg.device_id,
            on_persist=on_persist,
            on_refresh=on_refresh,
        )
    finally:
        subscriber.stop()
        # Also save on the way out, including after Ctrl-C, so a short run
        # still leaves the next start something to work with.
        if on_persist is not None:
            on_persist()
        _note_error_log(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # Before anything else can log: this is what stops a log record from
    # being printed over the dashboard by logging's stderr fallback.
    errorlog.configure(None if args.no_error_log else errorlog.resolve_log_path(args.error_log))
    try:
        if args.replay:
            return _run_replay(args)
        return _run_live(args)
    except KeyboardInterrupt:
        print("\nInterrupted, shutting down.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
