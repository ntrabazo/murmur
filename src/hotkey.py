"""Global hotkey listener for flow-clone (Chunk 1).

Uses pynput's ``win32_event_filter`` — the ONLY way to suppress just one key
(Listener ``suppress=True`` would swallow the entire keyboard).

Hook-thread budget (plan §3.5): everything dispatched from here runs on the
low-level keyboard hook thread, which has a hard sub-millisecond budget before
Windows silently unhooks it and stalls all system keyboard input. The wired
``on_press``/``on_release`` callbacks must therefore call ONLY
``recorder.arm()`` / ``recorder.disarm_and_get()`` (pure in-memory) plus a
``queue.put`` — never ``open_stream()``/``close_stream()``, never anything
blocking.

REQUIRED call order in the caller's on_release (plan §6 Chunk 1, resolves a
planner-flagged ambiguity):

    1. Unconditionally call ``recorder.disarm_and_get()`` FIRST — clearing
       armed state and retrieving whatever was captured, which may
       legitimately be an empty buffer on a very fast tap.
    2. Only AFTER that, check
       ``time.perf_counter() - listener.press_timestamp < DEBOUNCE_SEC``;
       if true, discard the returned audio and skip enqueueing.

    The debounce discards the *result* — it never skips calling
    ``disarm_and_get()``, which is exactly why that function tolerates an
    empty buffer on its own.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

# Windows keyboard messages seen by the low-level hook.
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104  # key with Alt held
WM_SYSKEYUP = 0x0105

#: Presses shorter than this are accidental taps — the caller discards the
#: (already-retrieved) audio rather than running the pipeline on 0.1s of it.
DEBOUNCE_SEC = 0.15

#: The cancel key (Escape). Not configurable on purpose — Esc-means-abort is
#: OS-wide muscle memory, and a second config knob would just be a foot-gun.
VK_ESCAPE = 0x1B


class DoubleTapTracker:
    """Pure timestamp-based double-tap detector for dictation_mode "dual".

    Hook-thread safe by construction: press() only compares and stores a
    float — none of the "never block/sleep" risk the module docstring
    warns about. Detection is PRESS-to-PRESS (not release-to-press): the
    gap that matters is between the two key-DOWNs, so a genuine hold's own
    (possibly very late) release never looks like a double-tap no matter
    how long the key was held.
    """

    def __init__(self, window_sec: float) -> None:
        self.window_sec = window_sec
        self.last_press: float | None = None

    def press(self, now: float) -> bool:
        """Record a press at ``now``; return True if it lands within
        ``window_sec`` of the immediately preceding press."""
        prev, self.last_press = self.last_press, now
        return prev is not None and (now - prev) < self.window_sec


def _vk_for(key_name: str) -> int:
    """Resolve a config key name ('f9', 'scroll_lock', 'a', ...) to a Windows VK code."""
    try:
        vk = keyboard.Key[key_name.lower()].value.vk
    except KeyError:
        vk = keyboard.KeyCode.from_char(key_name).vk
    if vk is None:
        raise ValueError(f"Cannot resolve hotkey {key_name!r} to a virtual-key code")
    return vk


class HotkeyListener:
    """Press/release dispatch for a single global hotkey, with per-key suppression.

    Exposes ``press_timestamp`` (``time.perf_counter()`` at the accepted
    key-down) so the caller's ``on_release`` can apply the DEBOUNCE_SEC check
    *after* it has called ``recorder.disarm_and_get()`` — see module docstring
    for the mandated call order.
    """

    def __init__(
        self,
        key_name: str,
        suppress: bool,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._vk = _vk_for(key_name)
        self._suppress = suppress
        self._on_press = on_press
        self._on_release = on_release
        # Esc-to-cancel (2026-07-10): on_cancel fires on Escape KEY-DOWN and
        # returns True when it CONSUMED the press (a dictation was cancelled)
        # — then, and only then, the Esc event is suppressed system-wide so
        # cancelling a dictation doesn't also close a dialog/menu in the
        # target app. Returning False passes Esc through untouched, so it
        # keeps working normally whenever Murmur is idle. Same hook-thread
        # sub-ms budget as on_press/on_release. Disabled if the main hotkey
        # IS Escape (the config would collide with itself).
        self._on_cancel = on_cancel if self._vk != VK_ESCAPE else None
        # When a cancel consumed the key-DOWN, swallow the matching key-UP
        # too — apps must never see a lone Esc key-up.
        self._cancel_down_consumed = False
        # Key-repeat guard: Windows auto-repeats WM_KEYDOWN while the key is
        # held; only the first down and the final up dispatch.
        self._down = False
        self.press_timestamp: float | None = None
        self._listener: keyboard.Listener | None = None

    def start(self) -> None:
        self._listener = keyboard.Listener(win32_event_filter=self._filter)
        self._listener.daemon = True
        self._listener.start()
        log.info("hotkey listener started (vk=0x%02X, suppress=%s)", self._vk, self._suppress)

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
            log.info("hotkey listener stopped")

    # ------------------------------------------------------------------ #
    # Low-level hook filter — runs on the hook thread, sub-ms budget.     #
    # ------------------------------------------------------------------ #

    def _filter(self, msg, data) -> bool:
        if data.vkCode == VK_ESCAPE and self._on_cancel is not None:
            consumed = False
            try:
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    consumed = bool(self._on_cancel())
                    self._cancel_down_consumed = consumed
                elif msg in (WM_KEYUP, WM_SYSKEYUP):
                    consumed = self._cancel_down_consumed
                    self._cancel_down_consumed = False
            except Exception:
                # Same contract as below: never propagate into the hook.
                log.exception("cancel callback raised")
            if consumed and self._listener is not None:
                self._listener.suppress_event()
            return True

        if data.vkCode != self._vk:
            return True  # every other key passes through untouched

        try:
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if not self._down:  # ignore Windows key-repeat
                    self._down = True
                    self.press_timestamp = time.perf_counter()
                    self._on_press()
            elif msg in (WM_KEYUP, WM_SYSKEYUP):
                if self._down:
                    self._down = False
                    self._on_release()
        except Exception:
            # A callback exception must never propagate into the low-level
            # hook — that would risk Windows unhooking us entirely.
            log.exception("hotkey callback raised")

        if self._suppress and self._listener is not None:
            # Raises pynput's internal suppress exception: the event is
            # swallowed system-wide (F9 never reaches the focused app).
            self._listener.suppress_event()
        return True
