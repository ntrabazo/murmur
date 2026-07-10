"""System tray icon for flow-clone (Chunk 4).

pystray Icon running in a daemon thread (plan §3.5). The icon is a
PIL-drawn colored dot:

    grey   = IDLE                      (ready, waiting for the hotkey)
    red    = RECORDING                 (mic armed)
    yellow = everything else           (transcribing / cleaning /
                                        injecting — "busy, hands off")

    blue   = paused                    (Chunk 7: mic stream closed, hotkey
                                        ignored — tray Resume to re-arm)

Menu actions run on the pystray thread; the callbacks passed in must
therefore be queue-puts or other thread-safe operations, never direct
tkinter calls (main.py wires them accordingly).

Pause/Resume (Chunk 7): the menu label is dynamic ("Pause" <-> "Resume"),
but the tray does NOT flip its own paused flag — main.py owns the real
paused state (Resume can FAIL if the mic disappeared, in which case the app
stays paused and the label must keep saying "Resume"). The click callback
just notifies main.py; main.py calls back into set_paused() once the toggle
actually happened.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from src.app_state import State

log = logging.getLogger(__name__)

_COLORS = {
    "grey": (128, 128, 128, 255),
    "red": (220, 60, 50, 255),
    "yellow": (235, 185, 30, 255),
    "blue": (70, 130, 200, 255),  # paused (Chunk 7)
}

_STATE_COLOR = {
    State.IDLE: "grey",
    State.RECORDING: "red",
    State.TRANSCRIBING: "yellow",
    State.CLEANING: "yellow",
    State.INJECTING: "yellow",
}


def _dot_icon(color: tuple[int, int, int, int]) -> Image.Image:
    """Draw a filled dot on a transparent square. Drawn at 64x64 so Windows'
    downscale to tray size stays crisp."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color, outline=(40, 40, 40, 255), width=3)
    return img


class Tray:
    """pystray icon wrapper: status dot, toast notifications, menu."""

    def __init__(
        self,
        on_toggle_pause: Callable[[], None],
        on_open_dictionary: Callable[[], None],
        on_open_app: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._icons = {name: _dot_icon(rgba) for name, rgba in _COLORS.items()}
        self._paused = False
        self._on_toggle_pause = on_toggle_pause

        menu = pystray.Menu(
            # v2 U4: the app window (transcripts + dictionary) is the face
            # of the tool — first item AND the icon's default action
            # (double-click the tray dot opens it). Like every callback
            # here this runs on the pystray thread — main.py wires it as a
            # ui_q put so the tkinter window is only ever touched from the
            # main thread. The old separate "History" item is absorbed.
            pystray.MenuItem("Open Murmur", lambda: on_open_app(),
                             default=True),
            pystray.MenuItem(
                lambda item: "Resume" if self._paused else "Pause",
                self._toggle_pause,
            ),
            # Raw-JSON escape hatch — reload_if_changed() reconciles hand
            # edits on the next dictation.
            pystray.MenuItem("Open dictionary", lambda: on_open_dictionary()),
            pystray.MenuItem("Quit", lambda: on_quit()),
        )
        self._icon = pystray.Icon(
            "Murmur", self._icons["grey"], "Murmur — starting…", menu
        )
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Run the pystray message loop in a daemon thread (plan §3.5)."""
        self._thread = threading.Thread(
            target=self._icon.run, name="tray", daemon=True
        )
        self._thread.start()
        log.info("tray icon thread started")

    def stop(self) -> None:
        """Stop the icon loop (thread-safe; called from the main thread on quit)."""
        try:
            self._icon.stop()
        except Exception:  # already stopped / never ran — quit must not die here
            log.exception("tray stop raised")

    # ------------------------------------------------------------------ #
    # Status + toasts (thread-safe: pystray marshals internally)           #
    # ------------------------------------------------------------------ #

    def set_status(self, state: State) -> None:
        # While paused, IDLE renders as the blue paused dot — an in-flight
        # dictation finishing (plan §6 Chunk 7: pause lets the current
        # pipeline complete) still shows its yellow/red states normally.
        if self._paused and state == State.IDLE:
            color, title = "blue", "Murmur — paused"
        else:
            color, title = _STATE_COLOR.get(state, "yellow"), f"Murmur — {state.name.lower()}"
        try:
            self._icon.icon = self._icons[color]
            self._icon.title = title
        except Exception:
            log.exception("tray set_status failed")

    def set_paused(self, paused: bool) -> None:
        """Called by main.py AFTER a pause/resume actually took effect
        (Chunk 7). Updates the dynamic menu label + the paused dot. The tray
        never flips this itself — see module docstring."""
        self._paused = paused
        try:
            self._icon.update_menu()  # re-evaluates the dynamic label
        except Exception:
            log.exception("tray update_menu failed")
        # Refresh the dot: paused shows blue; unpausing back to idle grey.
        self.set_status(State.IDLE)

    def toast(self, msg: str) -> None:
        """Balloon/toast notification. Never fatal — a lost toast is cosmetic."""
        try:
            if getattr(self._icon, "HAS_NOTIFICATION", True):
                self._icon.notify(msg, "Murmur")
            else:
                log.info("toast (no notification support): %s", msg)
        except Exception:
            log.exception("tray toast failed: %s", msg)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _toggle_pause(self) -> None:
        # Notify only — main.py performs the toggle (which can fail on
        # Resume) and reports the resulting state back via set_paused().
        log.info("tray pause/resume clicked (paused=%s at click time)", self._paused)
        self._on_toggle_pause()
