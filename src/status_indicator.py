"""Floating on-screen status pill (Chunk 4 amendment; redesigned v2 U5).

A small, smooth, dark, always-on-top pill at the bottom-center of the primary
screen — the Wispr-Flow-style visible status Nicolas wanted instead of the
buried tray dot. v2 U5 replaces the flat colored rectangle with:
  - a rounded dark pill (borderless window + ``-transparentcolor`` key so the
    corners are truly transparent and click-through), and
  - a LIVE level meter: a row of vertical bars that dance to the microphone
    input while RECORDING (driven by ``recorder.level``), and a gentle
    uniform "breathing" animation while the pipeline is TRANSCRIBING/CLEANING.
  - hidden entirely otherwise (IDLE/INJECTING show nothing).

Design contract (unchanged from Chunk 4, still load-bearing):
- ``overrideredirect(True)`` is safe here because this window NEVER takes
  keyboard focus — no ``focus_force()``/``grab_set()``, ever, or it would
  pull the caret out of the app being dictated into.
- All methods + the animation loop run on the tkinter main thread only.
  ``recorder.level`` is read unlocked; a single float read is GIL-atomic.
"""

from __future__ import annotations

import logging
import math
import tkinter as tk
from typing import Callable

from src.app_state import State

log = logging.getLogger(__name__)

_WIDTH = 208
_HEIGHT = 48
_MARGIN_BOTTOM = 64  # px above the bottom edge — clears a default taskbar
_RADIUS = _HEIGHT // 2

#: A color we never intentionally draw — every pixel of exactly this value is
#: made transparent + click-through by the window manager, which is how the
#: rounded corners read as "not there" instead of dark squares.
_CHROMA_KEY = "#FF00FE"

_PILL_BG = "#202124"      # near-black dark pill
_BAR_RECORD = "#FF5C5C"   # warm red bars while recording
_BAR_PROCESS = "#EBB91E"  # amber bars while processing

_N_BARS = 13
_BAR_W = 4
_BAR_GAP = 6
_BAR_MIN_H = 4            # resting height so bars never fully vanish
_BAR_MAX_H = _HEIGHT - 20  # tallest a bar can grow

_ANIM_MS = 33            # ~30fps, roughly one frame per audio chunk


def _round_rect_points(x0, y0, x1, y1, r):
    """Point list for a smooth()-ed rounded rectangle polygon."""
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


class StatusIndicator:
    """Rounded dark pill with a live level meter; never takes focus."""

    def __init__(self, root: tk.Tk,
                 level_provider: Callable[[], float] = lambda: 0.0) -> None:
        self._level_provider = level_provider
        self._mode: str | None = None     # "record" | "process" | None(hidden)
        self._animating = False
        self._phase = 0.0

        top = tk.Toplevel(root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.configure(bg=_CHROMA_KEY)
        # Make the key color transparent + click-through (Windows).
        try:
            top.attributes("-transparentcolor", _CHROMA_KEY)
        except tk.TclError:  # pragma: no cover - non-Windows fallback
            top.configure(bg=_PILL_BG)

        self._canvas = tk.Canvas(
            top, width=_WIDTH, height=_HEIGHT,
            bg=_CHROMA_KEY, highlightthickness=0, bd=0,
        )
        self._canvas.pack()
        self._top = top
        self._draw_pill()

    # ------------------------------------------------------------------ #
    # Public API — main thread only                                       #
    # ------------------------------------------------------------------ #

    def set_status(self, state: State) -> None:
        """Show a live meter for RECORDING/TRANSCRIBING/CLEANING; hide else."""
        if state is State.RECORDING:
            mode = "record"
        elif state in (State.TRANSCRIBING, State.CLEANING):
            mode = "process"
        else:
            mode = None

        if mode is None:
            self._mode = None
            self._top.withdraw()
            return

        self._mode = mode
        self._place_bottom_center()
        self._top.deiconify()
        self._top.lift()
        self._top.attributes("-topmost", True)  # re-assert after deiconify
        if not self._animating:
            self._animating = True
            self._animate()

    def hide(self) -> None:
        self._mode = None
        self._top.withdraw()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _place_bottom_center(self) -> None:
        sw = self._top.winfo_screenwidth()
        sh = self._top.winfo_screenheight()
        x = max(0, (sw - _WIDTH) // 2)
        y = max(0, sh - _HEIGHT - _MARGIN_BOTTOM)
        self._top.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

    def _draw_pill(self) -> None:
        """Draw the static rounded pill background once."""
        self._canvas.create_polygon(
            _round_rect_points(1, 1, _WIDTH - 1, _HEIGHT - 1, _RADIUS),
            fill=_PILL_BG, outline=_PILL_BG, smooth=True, tags="pill",
        )

    def _bar_heights(self) -> list[float]:
        """Per-bar heights (px) for the current frame and mode."""
        mid = (_N_BARS - 1) / 2.0
        heights: list[float] = []
        if self._mode == "record":
            level = max(0.0, min(1.0, self._level_provider()))
            for i in range(_N_BARS):
                # Center bars taller (envelope) + a little per-bar wobble so a
                # steady tone still shimmers instead of freezing flat.
                envelope = 1.0 - 0.6 * (abs(i - mid) / mid)
                wobble = 0.75 + 0.25 * math.sin(self._phase * 2.0 + i * 0.9)
                h = _BAR_MIN_H + level * _BAR_MAX_H * envelope * wobble
                heights.append(h)
        else:  # "process" — uniform gentle breathing, no mic input
            breathe = 0.5 + 0.5 * math.sin(self._phase * 1.6)
            h = _BAR_MIN_H + 0.35 * _BAR_MAX_H * breathe
            heights = [h] * _N_BARS
        return heights

    def _redraw_bars(self) -> None:
        self._canvas.delete("bar")
        color = _BAR_RECORD if self._mode == "record" else _BAR_PROCESS
        total_w = _N_BARS * _BAR_W + (_N_BARS - 1) * _BAR_GAP
        x = (_WIDTH - total_w) / 2.0
        cy = _HEIGHT / 2.0
        for h in self._bar_heights():
            half = h / 2.0
            self._canvas.create_rectangle(
                x, cy - half, x + _BAR_W, cy + half,
                fill=color, outline=color, tags="bar",
            )
            x += _BAR_W + _BAR_GAP

    def _animate(self) -> None:
        """Self-rescheduling redraw loop; stops when the pill is hidden."""
        if self._mode is None:
            self._animating = False
            return
        self._phase += _ANIM_MS / 1000.0 * math.tau
        self._redraw_bars()
        self._top.after(_ANIM_MS, self._animate)


# Re-exported for a headless test that drives one frame without a real screen.
__all__ = ["StatusIndicator"]
