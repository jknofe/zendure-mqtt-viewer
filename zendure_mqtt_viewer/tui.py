"""Curses runtime: blits a layout.Frame onto the real terminal and handles
keyboard input (tab switching, quit) and resize.

This module is intentionally thin - all layout decisions live in layout.py
and are unit-testable without curses. This file's only job is turning a
Frame into addstr() calls and turning keypresses into tab changes.
"""
from __future__ import annotations

import curses
import logging
import signal
import time
from typing import Callable, Optional

from . import layout
from .state import DashboardState

logger = logging.getLogger(__name__)

# layout.Span.attr is a space-separated token set, e.g. "bold ok" or
# "reverse accent bold". Emphasis tokens map straight to curses attributes;
# colour names map to colour pairs allocated at startup. Unknown tokens are
# ignored, so the layout can name a colour this file doesn't know without
# breaking the display.
EMPHASIS_MAP = {
    "normal": curses.A_NORMAL,
    "reverse": curses.A_REVERSE,
    "dim": curses.A_DIM,
    "bold": curses.A_BOLD,
    # "muted" is deliberately an attribute rather than a colour: a dimmed
    # foreground reads correctly on light and dark terminals alike, whereas
    # any fixed grey is invisible on one of them.
    "muted": curses.A_DIM,
}

COLOR_FG = {
    "accent": curses.COLOR_CYAN,
    "ok": curses.COLOR_GREEN,
    "warn": curses.COLOR_YELLOW,
    "error": curses.COLOR_RED,
    "info": curses.COLOR_MAGENTA,
}

_color_pairs: dict[str, int] = {}
_attr_cache: dict[str, int] = {}


def init_colors() -> bool:
    """Allocate one colour pair per named colour. True if colour is usable.

    Monochrome terminals fall through to emphasis-only rendering - the
    dashboard has to stay readable over a serial console or in TERM=dumb,
    which is exactly where you end up when something has gone wrong.
    """
    _color_pairs.clear()
    _attr_cache.clear()
    try:
        if not curses.has_colors():
            return False
        curses.start_color()
    except curses.error:
        return False

    background = -1
    try:
        curses.use_default_colors()  # keep the user's own terminal background
    except curses.error:
        background = curses.COLOR_BLACK

    for index, name in enumerate(sorted(COLOR_FG), start=1):
        try:
            curses.init_pair(index, COLOR_FG[name], background)
        except curses.error:
            continue  # ran out of pairs; the rest still render in emphasis
        _color_pairs[name] = curses.color_pair(index)
    return bool(_color_pairs)


def attr_for(attr: str) -> int:
    """Resolve a Span attr string to a curses attribute bitmask."""
    cached = _attr_cache.get(attr)
    if cached is not None:
        return cached
    value = curses.A_NORMAL
    for token in attr.split():
        if token in EMPHASIS_MAP:
            value |= EMPHASIS_MAP[token]
        elif token in _color_pairs:
            value |= _color_pairs[token]
    _attr_cache[attr] = value
    return value

# keys that switch tabs directly
_DIGIT_KEYS = {ord(str(i + 1)): i for i in range(len(layout.TABS))}
_QUIT_KEYS = {ord("q"), ord("Q")}
_REFRESH_KEYS = {ord("r"), ord("R")}

# How often to checkpoint last-seen values while running.
PERSIST_INTERVAL = 60.0

# Second, explicit Ctrl-C path: a signal arriving while blocked in a C-level
# getch() is noticed at the next loop iteration, rather than depending on
# exactly when that call gets interrupted.
_interrupted = False


def _handle_sigint(signum, frame) -> None:
    global _interrupted
    _interrupted = True


def _blit(stdscr, frame: layout.Frame) -> None:
    # frame was built from this same stdscr's current getmaxyx(), so it
    # should never be out of bounds - the try/except below is just a safety
    # net for the well-known bottom-right-corner curses quirk.
    for y, line in enumerate(frame.lines):
        x = 0
        for span in line:
            if not span.text:
                continue
            attr = attr_for(span.attr)
            try:
                # Writing the very last cell of the very last line can raise
                # curses.error on some terminals (auto-margin wrap) - never
                # let a cosmetic redraw glitch crash the tool.
                stdscr.addstr(y, x, span.text, attr)
            except curses.error:
                pass
            x += len(span.text)
    stdscr.noutrefresh()
    curses.doupdate()


def run(
    stdscr,
    state: DashboardState,
    *,
    mode: str,
    device_id: str = "",
    interval: float = 1.0,
    duration: Optional[float] = None,
    initial_tab: str = "overview",
    on_persist: Optional[Callable[[], object]] = None,
    on_refresh: Optional[Callable[[], object]] = None,
) -> None:
    """Main curses loop. Call via curses.wrapper() so the terminal is always
    restored, including on exception or Ctrl-C.

    ``on_persist`` (if given) is called every PERSIST_INTERVAL seconds to
    checkpoint last-seen values, so a kill -9 or a laptop lid closing still
    leaves a recent cache behind.

    ``on_refresh`` is what the ``r`` key calls: a request for a full
    report. Absent (replay, which has no broker), ``r`` does nothing.
    """
    global _interrupted
    _interrupted = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # terminals without a hideable cursor are still perfectly usable
    stdscr.nodelay(True)
    stdscr.timeout(150)  # ms - keeps key handling responsive between redraws

    try:
        tab_index = layout.TABS.index(next(t for t in layout.TABS if t.lower() == initial_tab.lower()))
    except StopIteration:
        tab_index = 0

    end_time = time.time() + duration if duration is not None else None
    last_draw = 0.0
    last_persist = time.time()
    need_redraw = True

    try:
        while True:
            if _interrupted:
                return

            now = time.time()

            if on_persist is not None and (now - last_persist) >= PERSIST_INTERVAL:
                last_persist = now
                try:
                    on_persist()
                except Exception as exc:  # pragma: no cover - a cache write
                    # must never take down a running dashboard, and must
                    # never say so on screen either. It goes in the log.
                    logger.warning("last-seen checkpoint failed: %s", exc)

            ch = stdscr.getch()
            if ch != -1:
                if ch in _QUIT_KEYS:
                    return
                elif ch in _DIGIT_KEYS:
                    tab_index = _DIGIT_KEYS[ch]
                    need_redraw = True
                elif ch in (curses.KEY_RIGHT, ord("\t")):
                    tab_index = (tab_index + 1) % len(layout.TABS)
                    need_redraw = True
                elif ch in (curses.KEY_LEFT, curses.KEY_BTAB):
                    tab_index = (tab_index - 1) % len(layout.TABS)
                    need_redraw = True
                elif ch in _REFRESH_KEYS and on_refresh is not None:
                    try:
                        on_refresh()
                    except Exception as exc:  # pragma: no cover - defensive
                        # A keypress must never take down the dashboard, and
                        # never print anything either. Log it and carry on.
                        logger.warning("refresh request raised: %s", exc)
                    need_redraw = True
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.clear()
                    need_redraw = True

            if need_redraw or (now - last_draw) >= interval:
                rows, cols = stdscr.getmaxyx()
                frame = layout.build_frame(
                    state,
                    layout.TABS[tab_index],
                    cols,
                    rows,
                    now,
                    mode=mode,
                    device_id=device_id,
                )
                _blit(stdscr, frame)
                last_draw = now
                need_redraw = False

            if end_time is not None and time.time() >= end_time:
                return
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
