"""Command-line entry point.

Wires together config loading, the MQTT subscriber (or --replay file
reader), the DashboardState, and the plain-text renderer. Terminal control
(clear/redraw, cursor hide/show) lives here, not in render.py, so the
renderer itself stays a pure, easily-testable function.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import render
from . import replay as replay_mod
from .mqtt_client import Subscriber
from .state import DashboardState

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_AND_HOME = "\x1b[H\x1b[2J"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zendure-mqtt-viewer",
        description=(
            "Read-only live ASCII dashboard for a Zendure SolarFlow hub over MQTT. "
            "This tool never publishes to the broker."
        ),
    )
    p.add_argument("--config", metavar="PATH", help="path to config.toml (default: ~/.config/zendure-mqtt-viewer/config.toml)")
    p.add_argument("--replay", metavar="FILE.jsonl", help="replay a capture file instead of connecting to a broker")
    p.add_argument("--replay-speed", type=float, default=0.0, help="pace replay using captured timestamps (0 = as fast as possible, 1.0 = real time)")
    p.add_argument("--once", action="store_true", help="render a single frame and exit (live mode waits briefly for a first message)")
    p.add_argument("--duration", type=float, metavar="SECONDS", help="live mode: run for this many seconds then exit, printing the final frame")
    p.add_argument("--interval", type=float, default=1.0, help="screen refresh interval in seconds (default 1.0)")
    p.add_argument("--width", type=int, help="override detected terminal width")
    return p


def _terminal_width(explicit: int | None) -> int:
    if explicit:
        return explicit
    return shutil.get_terminal_size(fallback=(100, 24)).columns


def _print_frame(state: DashboardState, mode: str, width: int) -> None:
    sys.stdout.write(render.render(state, width=width, now=time.time(), mode=mode))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _run_display_loop(state: DashboardState, mode: str, args: argparse.Namespace) -> None:
    """Shared render loop for both live and replay (post-load) display.

    Behavior:
      --once            -> one frame, exit.
      --duration N      -> refresh (if a tty) or print frames (if piped)
                            for N seconds, then a final frame, exit.
      interactive tty   -> redraw in place until Ctrl-C.
      piped / non-tty   -> print successive frames (separated) until
                            Ctrl-C, so `--duration` or a pipe consumer can
                            capture output non-interactively.
    """
    width = _terminal_width(args.width)
    tty = sys.stdout.isatty()

    if args.once:
        if mode == "live":
            # give the subscriber a brief window to receive a first message
            deadline = time.time() + min(5.0, args.duration or 5.0)
            while state.messages_received == 0 and time.time() < deadline:
                time.sleep(0.1)
        _print_frame(state, mode, width)
        return

    end_time = time.time() + args.duration if args.duration is not None else None

    if tty:
        sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            frame = render.render(state, width=width, now=time.time(), mode=mode)
            if tty:
                sys.stdout.write(CLEAR_AND_HOME)
                sys.stdout.write(frame)
            else:
                sys.stdout.write(frame)
                sys.stdout.write("\n" + ("-" * min(width, 40)) + "\n")
            sys.stdout.flush()
            if end_time is not None and time.time() >= end_time:
                break
            time.sleep(args.interval)
    finally:
        if tty:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()


def _run_replay(args: argparse.Namespace) -> int:
    path = Path(args.replay).expanduser()
    if not path.exists():
        print(f"Replay file not found: {path}", file=sys.stderr)
        return 2

    state = DashboardState()
    replay_mod.replay_file(path, state, speed=args.replay_speed)
    state.connected = True  # informational only; render() overrides wording in replay mode

    _run_display_loop(state, "replay", args)
    return 0


def _run_live(args: argparse.Namespace) -> int:
    try:
        cfg = config_mod.load_broker_config(args.config)
    except config_mod.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    state = DashboardState()
    subscriber = Subscriber(cfg, state)
    subscriber.start()
    try:
        _run_display_loop(state, "live", args)
    finally:
        subscriber.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.replay:
            return _run_replay(args)
        return _run_live(args)
    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        print("\nInterrupted, shutting down.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
