"""Headless verification for Esc-to-cancel (2026-07-10): the hook-thread
_on_cancel_press dispatch and the worker's stage-boundary cancel checks in
_handle_dictate/_do_inject — real AppState, fake Recorder/STT/Cleaner/
History (no mic, no pynput, no clipboard, no API).

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_cancel.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_cancel.py
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
from src.hotkey import DoubleTapTracker  # noqa: E402
from src.main import (  # noqa: E402
    DualModeState,
    _do_inject,
    _handle_dictate,
    _on_cancel_press,
    _on_hotkey_press,
)


class _FakeRecorder:
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


class _FakeStt:
    """transcribe() returns a fixed transcript; on_call lets a test set the
    cancel flag MID-transcription (Esc arriving while Whisper runs)."""

    def __init__(self, text: str = "hello world", on_call=None) -> None:
        self.text = text
        self.on_call = on_call
        self.calls = 0

    def transcribe(self, audio, initial_prompt=None):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return SimpleNamespace(text=self.text)


class _FakeCleaner:
    def __init__(self, text: str = "Hello world.", on_call=None) -> None:
        self.text = text
        self.on_call = on_call
        self.calls = 0

    def clean(self, raw, dictionary_block=""):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return SimpleNamespace(text=self.text, degraded=False, error=None)


class _FakeHistory:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []

    def add(self, text: str, outcome: str) -> None:
        self.added.append((text, outcome))


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _run_dictate(state, stt, cleaner, history, cancel_event,
                 fg_verdict=lambda hwnd: "other"):
    """_handle_dictate with headless fakes. The default fg_verdict says
    "other" so an uncancelled run ends at the no-paste History path without
    ever touching the real injector/clipboard."""
    ui_q: queue.Queue = queue.Queue()
    _handle_dictate(("dictate", "audio", 0), ui_q, stt, cleaner, state,
                    None, history=history, fg_verdict=fg_verdict,
                    cancel_event=cancel_event)
    return _drain(ui_q)


# ---------------------------------------------------------------------- #
# 1. Hook-thread dispatch (_on_cancel_press)                              #
# ---------------------------------------------------------------------- #

def test_esc_while_idle_passes_through() -> None:
    state = AppState()
    recorder = _FakeRecorder()
    dual = DualModeState()
    cancel_event = threading.Event()
    ui_q: queue.Queue = queue.Queue()

    assert _on_cancel_press(state, recorder, dual, cancel_event, ui_q) is False
    assert state.state is State.IDLE
    assert not cancel_event.is_set()
    assert recorder.disarm_count == 0
    assert _drain(ui_q) == [], "an idle Esc must be completely inert"


def test_esc_while_recording_discards_and_consumes() -> None:
    state = AppState()
    state.force(State.RECORDING)
    recorder = _FakeRecorder()
    dual = DualModeState()
    cancel_event = threading.Event()
    ui_q: queue.Queue = queue.Queue()

    assert _on_cancel_press(state, recorder, dual, cancel_event, ui_q) is True
    assert state.state is State.IDLE
    assert recorder.disarm_count == 1, "audio retrieved (and discarded)"
    assert not cancel_event.is_set(), "recording cancel never flags the worker"
    msgs = _drain(ui_q)
    assert ("status", State.IDLE) in msgs
    assert any(m[0] == "toast" and "cancelled" in m[1] for m in msgs), msgs


def test_esc_while_latched_clears_dual_bookkeeping() -> None:
    """A hands-free (double-tap latched) recording dies to Esc too, and the
    dual-mode watchdog must find nothing left to resolve."""
    state = AppState()
    state.force(State.RECORDING)
    recorder = _FakeRecorder()
    dual = DualModeState()
    dual.latched = True
    dual.pending_release_at = 42.0  # worst case: both set
    cancel_event = threading.Event()
    ui_q: queue.Queue = queue.Queue()

    assert _on_cancel_press(state, recorder, dual, cancel_event, ui_q) is True
    assert state.state is State.IDLE
    assert dual.latched is False
    assert dual.pending_release_at is None


def test_esc_mid_pipeline_flags_the_worker() -> None:
    for busy in (State.TRANSCRIBING, State.CLEANING, State.INJECTING):
        state = AppState()
        state.force(busy)
        recorder = _FakeRecorder()
        cancel_event = threading.Event()
        ui_q: queue.Queue = queue.Queue()

        assert _on_cancel_press(state, recorder, DualModeState(),
                                cancel_event, ui_q) is True
        assert cancel_event.is_set(), busy
        assert recorder.disarm_count == 0, "nothing armed to discard"
        assert state.state is busy, "the WORKER owns the transition to IDLE"


def test_new_recording_clears_stale_cancel_flag() -> None:
    """An Esc that raced in after its pipeline already finished must not
    kill the NEXT dictation — arming a fresh recording clears the flag."""
    state = AppState()
    recorder = _FakeRecorder()
    cancel_event = threading.Event()
    cancel_event.set()  # stale — set while nothing was in flight anymore
    job_q, ui_q = queue.Queue(), queue.Queue()

    _on_hotkey_press(state, recorder, DualModeState(),
                     DoubleTapTracker(0.35), threading.Event(), job_q, ui_q,
                     "hold", 90, now=0.0, cancel_event=cancel_event)

    assert state.state is State.RECORDING
    assert not cancel_event.is_set()


# ---------------------------------------------------------------------- #
# 2. Worker stage-boundary drops                                          #
# ---------------------------------------------------------------------- #

def test_cancel_before_stt_drops_without_history() -> None:
    """Flag already set when the job is picked up: no transcription, no
    History entry (there is no text to keep), straight back to IDLE."""
    state = AppState()
    state.force(State.TRANSCRIBING)
    stt, cleaner, history = _FakeStt(), _FakeCleaner(), _FakeHistory()
    cancel_event = threading.Event()
    cancel_event.set()

    msgs = _run_dictate(state, stt, cleaner, history, cancel_event)

    assert stt.calls == 0 and cleaner.calls == 0
    assert history.added == []
    assert state.state is State.IDLE
    assert not cancel_event.is_set(), "flag consumed, not left to leak"
    assert any(m[0] == "toast" and "cancelled" in m[1] for m in msgs), msgs


def test_cancel_during_stt_keeps_transcript_in_history() -> None:
    """Esc lands while Whisper runs: the transcript exists, so it is
    recoverable from History as 'discarded' — but Claude is never called
    and nothing pastes."""
    state = AppState()
    state.force(State.TRANSCRIBING)
    cancel_event = threading.Event()
    stt = _FakeStt(text="hello world", on_call=cancel_event.set)
    cleaner, history = _FakeCleaner(), _FakeHistory()

    _run_dictate(state, stt, cleaner, history, cancel_event,
                 fg_verdict=lambda hwnd: (_ for _ in ()).throw(
                     AssertionError("paste path must not be reached")))

    assert cleaner.calls == 0
    assert history.added == [("hello world", "discarded")]
    assert state.state is State.IDLE


def test_cancel_during_clean_keeps_cleaned_text_in_history() -> None:
    state = AppState()
    state.force(State.TRANSCRIBING)
    cancel_event = threading.Event()
    stt = _FakeStt(text="hello world")
    cleaner = _FakeCleaner(text="Hello world.", on_call=cancel_event.set)
    history = _FakeHistory()

    _run_dictate(state, stt, cleaner, history, cancel_event,
                 fg_verdict=lambda hwnd: (_ for _ in ()).throw(
                     AssertionError("paste path must not be reached")))

    assert history.added == [("Hello world.", "discarded")]
    assert state.state is State.IDLE


def test_cancel_at_inject_boundary_skips_paste() -> None:
    """_do_inject's own check: flag set right before injection — the
    foreground verdict (and everything after it) is never consulted."""
    state = AppState()
    state.force(State.INJECTING)
    history = _FakeHistory()
    cancel_event = threading.Event()
    cancel_event.set()
    ui_q: queue.Queue = queue.Queue()

    _do_inject("Hello world.", 0, ui_q, state, 300, "paste", history, None,
               fg_verdict=lambda hwnd: (_ for _ in ()).throw(
                   AssertionError("verdict must not be consulted")),
               cancel_event=cancel_event)

    assert history.added == [("Hello world.", "discarded")]
    assert state.state is State.IDLE


def test_no_cancel_flows_to_inject_normally() -> None:
    """Plumbing sanity: with the flag never set, the dictation reaches the
    foreground verdict exactly as before (verdict 'other' -> History
    'window_changed', no paste — the headless-safe endpoint)."""
    state = AppState()
    state.force(State.TRANSCRIBING)
    stt, cleaner, history = _FakeStt(), _FakeCleaner(), _FakeHistory()
    verdicts = []

    def fg_verdict(hwnd):
        verdicts.append(hwnd)
        return "other"

    _run_dictate(state, stt, cleaner, history, threading.Event(), fg_verdict)

    assert stt.calls == 1 and cleaner.calls == 1
    assert verdicts == [0]
    assert history.added == [("Hello world.", "window_changed")]
    assert state.state is State.IDLE


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
