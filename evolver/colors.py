"""Tiny ANSI color helper for the CLI (no dependency).

Colors are auto-enabled on a TTY (and Windows consoles get VT processing turned
on), disabled when piped or when ``NO_COLOR`` is set; force with
``EVOLVER_COLOR=1``/``0``.
"""

from __future__ import annotations

import os
import sys

_CODES = {
    "green": "32", "red": "31", "yellow": "33", "cyan": "36",
    "magenta": "35", "blue": "34", "dim": "2", "bold": "1",
}


def _enabled() -> bool:
    force = os.environ.get("EVOLVER_COLOR")
    if force is not None:
        return force.lower() not in ("0", "false", "no", "")
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":  # enable ANSI on Windows consoles
        try:
            import ctypes

            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001
            return False
    return True


ENABLED = _enabled()


def paint(text, *styles: str) -> str:
    if not ENABLED or not styles:
        return str(text)
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\033[{codes}m{text}\033[0m" if codes else str(text)


def green(t) -> str:
    return paint(t, "green")


def red(t) -> str:
    return paint(t, "red")


def yellow(t) -> str:
    return paint(t, "yellow")


def cyan(t) -> str:
    return paint(t, "cyan")


def dim(t) -> str:
    return paint(t, "dim")


def bold(t) -> str:
    return paint(t, "bold")


def pnl(value: float, text=None) -> str:
    """Color a number by sign (green positive, red negative, dim zero)."""
    s = text if text is not None else f"{value:+.2f}"
    if value > 0:
        return green(s)
    if value < 0:
        return red(s)
    return dim(s)
