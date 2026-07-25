"""Curses runtime: blits a layout.Frame onto the real terminal and handles
keyboard input (tab switching, quit) and resize.

This module is intentionally thin - all layout decisions live in layout.py
and are unit-testable without curses. This file's only job is turning a
Frame into addstr() calls and turning keypresses into tab changes.
"""
from __future__ import annotations

import curses
import signal
import time
from typing import Optional

from . import layout
from .state import DashboardState

ATTR_MAP = {
    "normal": curses.A_NORMAL,
    "reverse": curses.A_REVERSE,
    "dim": curses.A_DIM,
    "bold": curses.A_BOLD,
}

# keys that switch tabs directly
_DIGIT_KEYS = {ord(str(i + 1)): i for i in range(len(layout.TABS))}
_QUIT_KEYS = {ord("q"), ord("Q")}

# Belt-and-suspenders Ctrl-C handling: Python's default SIGINT handler turns
# it into a KeyboardInterrupt raised between bytecode instructions, which
# normally propagates fine through curses.wrapper's try/finally. This flag
# is a second, more explicit path so a signal arriving while blocked inside
# a C-level getch() call is still noticed at the very next loop iteration
# instead of depending on exactly when/whether that call gets interrupted.
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
            attr = ATTR_MAP.get(span.attr, curses.A_NORMAL)
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
) -> None:
    """Main curses loop. Call via curses.wrapper() so the terminal is always
    restored, including on exception or Ctrl-C.
    """
    global _interrupted
    _interrupted = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(150)  # ms - keeps key handling responsive between redraws

    try:
        tab_index = layout.TABS.index(next(t for t in layout.TABS if t.lower() == initial_tab.lower()))
    except StopIteration:
        tab_index = 0

    end_time = time.time() + duration if duration is not None else None
    last_draw = 0.0
    need_redraw = True

    try:
        while True:
            if _interrupted:
                return

            now = time.time()

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
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.clear()
                    need_redraw = True

            if need_redraw or (now - last_draw) >= interval:
                rows, cols = stdscr.getmaxyx()
                frame = layout.build_frame(
                    state, layout.TABS[tab_index], cols, rows, now, mode=mode, device_id=device_id
                )
                _blit(stdscr, frame)
                last_draw = now
                need_redraw = False

            if end_time is not None and time.time() >= end_time:
                return
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
