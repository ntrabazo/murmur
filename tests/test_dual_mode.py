"""Headless verification for dictation_mode "dual" (double-tap latch on
top of hold-to-talk): the pure DoubleTapTracker, the on_press/on_release
dispatch functions, and the poll_ui_queue watchdog — all with a real
AppState but a fake Recorder/HotkeyListener (no mic, no pynput, no tk).

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_dual_mode.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_dual_mode.py
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
from src.hotkey import DEBOUNCE_SEC, DoubleTapTracker  # noqa: E402
from src.main import (  # noqa: E402
    DualModeState,
    _dual_mode_watchdog_tick,
    _on_hotkey_press,
    _on_hotkey_release,
)

DOUBLE_TAP_SEC = 0.35
MAX_RECORDING_SEC = 90


class _FakeRecorder:
    """Pure in-memory stand-in for Recorder — arm()/disarm_and_get() mirror
    the real hot-path contract (clear-then-set on arm, atomic-swap on
    disarm) without touching PortAudio."""

    def __init__(self) -> None:
        self.overflowed = False
        self.arm_count = 0
        self.disarm_count = 0

    def arm(self) -> None:
        self.overflowed = False
        self.arm_count += 1

    def disarm_and_get(self):
        self.disarm_count += 1
        return f"audio#{self.disarm_count}"


class _FakeListener:
    def __init__(self, press_timestamp: float | None = None) -> None:
        self.press_timestamp = press_timestamp


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, mode, now):
    _on_hotkey_press(state, recorder, dual, tap_tracker, paused, job_q, ui_q,
                     mode, MAX_RECORDING_SEC, now=now)


def _release(state, recorder, dual, listener, job_q, ui_q, mode, now):
    _on_hotkey_release(state, recorder, dual, listener, job_q, ui_q, mode,
                       DOUBLE_TAP_SEC, MAX_RECORDING_SEC, now=now)


# ---------------------------------------------------------------------- #
# 1. DoubleTapTracker — pure timestamp classifier                         #
# ---------------------------------------------------------------------- #

def test_tracker_first_press_is_never_a_double_tap() -> None:
    t = DoubleTapTracker(DOUBLE_TAP_SEC)
    assert t.press(100.0) is False


def test_tracker_within_window_is_a_double_tap() -> None:
    t = DoubleTapTracker(DOUBLE_TAP_SEC)
    t.press(100.0)
    assert t.press(100.0 + DOUBLE_TAP_SEC - 0.01) is True


def test_tracker_at_or_past_window_is_not_a_double_tap() -> None:
    t = DoubleTapTracker(DOUBLE_TAP_SEC)
    t.press(100.0)
    assert t.press(100.0 + DOUBLE_TAP_SEC + 0.01) is False  # just past the window
    # last_press now points at that press, not the original one
    assert t.press(100.0 + DOUBLE_TAP_SEC + 5.0) is False  # miles away


def test_tracker_tracks_most_recent_press_only() -> None:
    t = DoubleTapTracker(DOUBLE_TAP_SEC)
    t.press(0.0)
    t.press(10.0)          # far gap — resets the reference point
    assert t.press(10.1) is True  # close to the SECOND press, not the first


# ---------------------------------------------------------------------- #
# 2. dictation_mode "hold" — byte-for-byte original behavior              #
# ---------------------------------------------------------------------- #

def test_hold_mode_plain_press_release_unaffected_by_dual_plumbing() -> None:
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "hold", 0.0)
    assert state.state is State.RECORDING
    assert recorder.arm_count == 1
    assert dual.latched is False  # "hold" never latches, regardless of timing

    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "hold", 2.0)  # long hold

    assert state.state is State.TRANSCRIBING
    jobs = _drain(job_q)
    assert len(jobs) == 1 and jobs[0][0] == "dictate"
    assert recorder.disarm_count == 1


def test_hold_mode_quick_tap_still_debounce_discarded() -> None:
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "hold", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "hold", DEBOUNCE_SEC / 2)

    assert state.state is State.IDLE
    assert _drain(job_q) == []
    assert recorder.disarm_count == 1  # still called unconditionally


def test_hold_mode_two_quick_presses_never_latch() -> None:
    """Even two presses inside double_tap_sec must behave as two ordinary
    hold-cycles in "hold" mode — the double-tap machinery must never
    activate outside dictation_mode == "dual"."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "hold", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "hold", 0.2)  # >DEBOUNCE
    assert state.state is State.TRANSCRIBING
    assert dual.latched is False
    state.force(State.IDLE)  # simulate the worker finishing

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "hold", 0.25)
    assert state.state is State.RECORDING
    assert dual.latched is False  # still never latches in hold mode


# ---------------------------------------------------------------------- #
# 3. dictation_mode "dual" — the double-tap latch                        #
# ---------------------------------------------------------------------- #

def test_dual_mode_fast_double_tap_latches_and_keeps_recording_continuous() -> None:
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    # press1 (fresh start — not a double-tap yet)
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    assert state.state is State.RECORDING
    assert dual.latched is False
    assert recorder.arm_count == 1
    _drain(ui_q)  # the initial ("status", RECORDING) from press1 — not under test

    # release1: a quick tap (well under double_tap_sec) -> deferred, NOT disarmed
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 0.08)
    assert state.state is State.RECORDING, "must stay armed through the gap"
    assert recorder.disarm_count == 0, "never disarmed across a deferred release"
    assert dual.pending_release_at == 0.08

    # press2 within double_tap_sec of press1 -> confirms the latch
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.2)
    assert dual.latched is True
    assert dual.pending_release_at is None
    assert recorder.arm_count == 1, "never re-armed — one continuous capture"
    assert _drain(ui_q) == [], "no extra status noise on the confirming press"

    # release2 (key-up of the confirming press): recording continues hands-free
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 0.25)
    assert state.state is State.RECORDING
    assert recorder.disarm_count == 0

    # press3: the stop-tap, any time later, regardless of its own duration
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 9.0)
    assert dual.latched is False
    assert state.state is State.TRANSCRIBING
    jobs = _drain(job_q)
    assert len(jobs) == 1 and jobs[0][0] == "dictate"
    assert recorder.disarm_count == 1, "disarmed exactly once, at the stop-tap"

    # release3 (key-up of the stop-tap) must be a pure no-op
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 9.05)
    assert state.state is State.TRANSCRIBING
    assert recorder.disarm_count == 1
    assert dual.pending_release_at is None


def test_dual_mode_genuine_long_hold_never_defers() -> None:
    """A hold clearly longer than double_tap_sec must finalize immediately
    on release — zero added latency for ordinary hold-to-talk use."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 3.0)

    assert state.state is State.TRANSCRIBING
    assert dual.pending_release_at is None
    assert recorder.disarm_count == 1
    assert len(_drain(job_q)) == 1


def test_dual_mode_lone_quick_tap_resolves_via_watchdog_as_discard() -> None:
    """No confirming second press arrives — the watchdog must eventually
    resolve the deferred release using the ORIGINAL held duration, so a
    genuinely accidental micro-tap still gets debounce-discarded."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "dual",
            DEBOUNCE_SEC / 2)  # tiny brush, well under DEBOUNCE_SEC too
    assert dual.pending_release_at == DEBOUNCE_SEC / 2

    # watchdog ticks before the window elapses: nothing happens yet
    _dual_mode_watchdog_tick(state, recorder, dual, listener, job_q, ui_q,
                             DOUBLE_TAP_SEC, MAX_RECORDING_SEC,
                             now=DEBOUNCE_SEC / 2 + 0.1)
    assert state.state is State.RECORDING
    assert dual.pending_release_at is not None

    # watchdog ticks after double_tap_sec + grace has elapsed: resolves
    _dual_mode_watchdog_tick(state, recorder, dual, listener, job_q, ui_q,
                             DOUBLE_TAP_SEC, MAX_RECORDING_SEC,
                             now=DEBOUNCE_SEC / 2 + DOUBLE_TAP_SEC + 0.1)
    assert dual.pending_release_at is None
    assert state.state is State.IDLE, "discarded — original hold was < DEBOUNCE_SEC"
    assert _drain(job_q) == []
    assert recorder.disarm_count == 1


def test_dual_mode_lone_quick_tap_resolves_via_watchdog_as_transcribe() -> None:
    """A tap held longer than DEBOUNCE_SEC but shorter than double_tap_sec,
    with no follow-up press, must still get transcribed once the watchdog
    resolves it — same outcome hold-mode's immediate release would give,
    just delayed by the double-tap ambiguity window."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    held = DEBOUNCE_SEC + 0.05
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", held)
    assert dual.pending_release_at == held

    _dual_mode_watchdog_tick(state, recorder, dual, listener, job_q, ui_q,
                             DOUBLE_TAP_SEC, MAX_RECORDING_SEC,
                             now=held + DOUBLE_TAP_SEC + 0.1)
    assert state.state is State.TRANSCRIBING
    assert len(_drain(job_q)) == 1


def test_dual_mode_watchdog_autostops_overflowed_latch() -> None:
    """plan §5.5: a latched recording left running has no release event to
    catch max_recording_sec overflow — the watchdog must do it."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 0.05)
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.2)
    assert dual.latched is True

    recorder.overflowed = True  # the Recorder callback would set this at max_sec
    _dual_mode_watchdog_tick(state, recorder, dual, listener, job_q, ui_q,
                             DOUBLE_TAP_SEC, MAX_RECORDING_SEC, now=90.2)

    assert dual.latched is False
    assert state.state is State.TRANSCRIBING
    jobs = _drain(job_q)
    assert len(jobs) == 1
    ui_msgs = _drain(ui_q)
    assert any(m[0] == "toast" and "limit" in m[1] for m in ui_msgs), ui_msgs


def test_dual_mode_stop_tap_ignores_pause() -> None:
    """Pause must never strand an active latched recording — stopping it
    is symmetric with hold-mode's on_release, which isn't pause-gated
    either (only STARTING a new recording is blocked)."""
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    listener = _FakeListener(press_timestamp=0.0)
    _release(state, recorder, dual, listener, job_q, ui_q, "dual", 0.05)
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.2)
    assert dual.latched is True

    paused.set()  # tray-paused while hands-free recording is in progress
    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 5.0)

    assert dual.latched is False
    assert state.state is State.TRANSCRIBING
    assert len(_drain(job_q)) == 1


def test_dual_mode_new_recording_blocked_while_paused() -> None:
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(DOUBLE_TAP_SEC)
    paused = threading.Event()
    paused.set()
    job_q, ui_q = queue.Queue(), queue.Queue()

    _press(state, recorder, dual, tap_tracker, paused, job_q, ui_q, "dual", 0.0)
    assert state.state is State.IDLE
    assert recorder.arm_count == 0


# ---------------------------------------------------------------------- #
# Plain-python runner                                                     #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
