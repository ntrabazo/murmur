"""Headless v2 U3 verification: the UIA correction watcher with all UIA
fully stubbed — the suite constructs zero COM/UIA objects (the thread's
CoInitialize/CoUninitialize pair runs, which is harmless without UIA calls).

Covers:
  - find_pasted_region (pure): exact containment, the corrected-word fuzzy
    case, unrelated text -> None, clamping at doc start/end, expand-to-
    whitespace at window edges;
  - the watcher thread with injected read/foreground fakes: stability gate
    (changing text never evaluates; stable text does), one-shot
    ("learned_corrections", ...) then thread death, supersession via the
    cancel Event, foreground-departure stop, a raising reader dying
    silently, stable-but-unchanged text posting nothing;
  - worker-level spawn wiring in main._do_inject: spawned only on res.ok,
    previous watcher superseded before the next spawn, nothing spawned on
    a skipped ("other") or failed paste or when disabled.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_correction_watcher.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_correction_watcher.py
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
from src.cleanup import CleanResult  # noqa: E402
from src.correction_watcher import CorrectionWatcher, find_pasted_region  # noqa: E402
from src.diff_learner import Correction  # noqa: E402
from src.injector import InjectResult  # noqa: E402
from src.stt import SttResult  # noqa: E402

PASTED = "talk to whisper flow"
CORRECTED = "talk to Wispr Flow"


# ---------------------------------------------------------------------- #
# 1. find_pasted_region — pure                                            #
# ---------------------------------------------------------------------- #

def test_region_exact_containment() -> None:
    doc = "notes so far. talk to whisper flow about the demo. done."
    assert find_pasted_region(doc, PASTED) == PASTED
    # Whole-control case too (the common single-line edit box).
    assert find_pasted_region(PASTED, PASTED) == PASTED


def test_region_fuzzy_corrected_word() -> None:
    """The case the watcher exists for: the user fixed a word in place, so
    exact containment fails but the fuzzy window must find the edited text."""
    region = find_pasted_region(CORRECTED, PASTED)
    assert region is not None
    assert "Wispr Flow" in region, region
    # Embedded in a larger document.
    doc = f"meeting notes: {CORRECTED} tomorrow."
    region = find_pasted_region(doc, PASTED)
    assert region is not None
    assert "Wispr Flow" in region, region


def test_region_unrelated_text_none() -> None:
    assert find_pasted_region(
        "the quarterly budget numbers look wrong to me", PASTED) is None
    assert find_pasted_region("", PASTED) is None
    assert find_pasted_region("whatever", "") is None


def test_region_clamps_at_doc_bounds() -> None:
    # Paste (edited) sits at the very START of the control text — the
    # 1.25x window must clamp, not raise / go negative.
    region = find_pasted_region("Wispr Flow", "whisper flow")
    assert region is not None and "Wispr Flow" in region, region
    # And at the very END.
    doc = "reminder: " + CORRECTED
    region = find_pasted_region(doc, PASTED)
    assert region is not None
    assert region.endswith("Wispr Flow"), region


def test_region_expands_to_whitespace() -> None:
    """A window edge landing mid-word must expand to the word boundary —
    the diff learner must never see half a token."""
    doc = "PREFIX " + CORRECTED + " suffixword"
    region = find_pasted_region(doc, PASTED)
    assert region is not None
    for token in region.split():
        assert token in doc.split(), \
            f"region token {token!r} is a fragment of the doc: {region!r}"


# ---------------------------------------------------------------------- #
# 2. Watcher thread — fakes only, poll_sec=0.01                           #
# ---------------------------------------------------------------------- #

HWND = 0x1111


def _make_watcher(job_q, cancel, read, foreground=lambda: HWND,
                  duration_sec: float = 5.0) -> CorrectionWatcher:
    return CorrectionWatcher(
        HWND, PASTED, job_q, cancel,
        poll_sec=0.01, duration_sec=duration_sec, min_similarity=0.45,
        read_focused_text=read, get_foreground=foreground,
    )


def _run(watcher: CorrectionWatcher, timeout: float = 10.0) -> None:
    watcher.start()
    watcher.join(timeout=timeout)
    assert not watcher.is_alive(), "watcher did not exit"


def test_watcher_stable_correction_fires_once() -> None:
    """Corrected text stable across polls -> exactly one
    ("learned_corrections", [...]) job, then the thread ends (one-shot)."""
    job_q: queue.Queue = queue.Queue()
    _run(_make_watcher(job_q, threading.Event(), read=lambda: CORRECTED))

    job = job_q.get_nowait()
    assert job[0] == "learned_corrections"
    assert job[1] == [Correction("whisper flow", "Wispr Flow")], job
    try:
        extra = job_q.get_nowait()
        raise AssertionError(f"watcher must fire ONCE, also got {extra}")
    except queue.Empty:
        pass


def test_watcher_changing_text_never_evaluates() -> None:
    """The stability gate: text that changes every poll (user still typing)
    posts nothing within the whole watch duration."""
    job_q: queue.Queue = queue.Queue()
    calls = {"n": 0}

    def churning_read() -> str:
        calls["n"] += 1
        return f"{CORRECTED} draft{calls['n']}"  # region differs every poll

    _run(_make_watcher(job_q, threading.Event(), read=churning_read,
                       duration_sec=0.15))
    assert calls["n"] >= 2, "watcher must have polled repeatedly"
    assert job_q.empty(), "changing text must never be evaluated"


def test_watcher_unchanged_text_posts_nothing() -> None:
    """Stable text that still equals the paste -> keep watching quietly
    until the duration expires; nothing is ever posted."""
    job_q: queue.Queue = queue.Queue()
    _run(_make_watcher(job_q, threading.Event(), read=lambda: PASTED,
                       duration_sec=0.15))
    assert job_q.empty()


def test_watcher_supersession_stops() -> None:
    """cancel.set() (a newer dictation pasted) -> prompt exit, no job —
    even though the control shows a learnable correction."""
    job_q: queue.Queue = queue.Queue()
    cancel = threading.Event()
    cancel.set()
    _run(_make_watcher(job_q, cancel, read=lambda: CORRECTED), timeout=5.0)
    assert job_q.empty(), "a superseded watcher must never post"


def test_watcher_foreground_departure_stops() -> None:
    """User left the target window -> stop silently FOR GOOD (no learn even
    though the reader would show a correction)."""
    job_q: queue.Queue = queue.Queue()
    reads = {"n": 0}

    def counting_read() -> str:
        reads["n"] += 1
        return CORRECTED

    _run(_make_watcher(job_q, threading.Event(), read=counting_read,
                       foreground=lambda: 0x2222))
    assert job_q.empty()
    assert reads["n"] == 0, "must stop BEFORE reading the departed window"


def test_watcher_raising_reader_dies_silently() -> None:
    """Any reader exception (hostile control, COM error) kills the watch
    silently — no job, no escape from the thread."""
    job_q: queue.Queue = queue.Queue()

    def bad_read() -> str:
        raise RuntimeError("boom (simulated COMError)")

    _run(_make_watcher(job_q, threading.Event(), read=bad_read))
    assert job_q.empty()


def test_watcher_unobservable_control_stops() -> None:
    """Reader returns None (neither TextPattern nor ValuePattern) -> stop
    silently forever."""
    job_q: queue.Queue = queue.Queue()
    _run(_make_watcher(job_q, threading.Event(), read=lambda: None))
    assert job_q.empty()


# ---------------------------------------------------------------------- #
# 3. Worker-level spawn wiring (main._do_inject tail)                     #
# ---------------------------------------------------------------------- #

class _StubStt:
    def __init__(self, text: str = PASTED) -> None:
        self.text = text

    def transcribe(self, audio, initial_prompt=None):
        return SttResult(text=self.text, audio_sec=1.0, latency_sec=0.1)


class _EchoCleaner:
    def clean(self, raw, dictionary_block=""):
        return CleanResult(text=raw, degraded=False, latency_sec=0.1)


class _FakeWatcherThread:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True


def _run_worker(job_q, ui_q, state, **kwargs) -> None:
    from src.main import pipeline_worker
    t = threading.Thread(
        target=pipeline_worker,
        args=(job_q, ui_q, _StubStt(), _EchoCleaner(), state),
        kwargs=kwargs, daemon=True,
    )
    t.start()
    job_q.put(None)
    t.join(timeout=10)
    assert not t.is_alive(), "worker did not exit on sentinel"


def test_worker_spawns_and_supersedes_watcher() -> None:
    """Two successful pastes -> two watcher spawns, and the FIRST watcher's
    cancel Event is set before the second spawn (one live watcher at a
    time). paste_into is patched to a headless success."""
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)

    spawned: list[dict] = []

    def factory(hwnd, text, q, cancel, poll_sec, duration_sec, min_similarity):
        w = _FakeWatcherThread()
        spawned.append({"hwnd": hwnd, "text": text, "job_q": q,
                        "cancel": cancel, "poll_sec": poll_sec,
                        "duration_sec": duration_sec,
                        "min_similarity": min_similarity, "thread": w})
        return w

    job_q.put(("dictate", object(), 0xAAAA))
    job_q.put(("dictate", object(), 0xBBBB))

    with mock.patch("src.main.paste_into",
                    return_value=InjectResult(ok=True, clipboard_restored=True,
                                              reason=None)):
        # Re-arm the state machine for the second job by forcing the path:
        # the worker force-resets nothing here, but each dictate starts in
        # TRANSCRIBING; after job 1 lands in IDLE the CAS calls in job 2
        # simply fail — harmless, the inject path still runs.
        _run_worker(job_q, ui_q, state,
                    fg_verdict=lambda h: "same",
                    watcher_factory=factory,
                    watcher_poll_sec=1.5, watcher_duration_sec=30.0,
                    min_similarity=0.5)

    assert len(spawned) == 2, spawned
    assert spawned[0]["hwnd"] == 0xAAAA and spawned[1]["hwnd"] == 0xBBBB
    assert spawned[0]["text"] == PASTED
    assert spawned[0]["job_q"] is job_q
    assert spawned[0]["poll_sec"] == 1.5
    assert spawned[0]["duration_sec"] == 30.0
    assert spawned[0]["min_similarity"] == 0.5
    assert all(s["thread"].started for s in spawned)
    # Supersession: spawning #2 set #1's cancel; #2's is still live.
    assert spawned[0]["cancel"].is_set(), "first watcher must be superseded"
    assert not spawned[1]["cancel"].is_set()
    assert spawned[0]["cancel"] is not spawned[1]["cancel"]


def test_worker_no_watcher_on_skipped_failed_or_disabled() -> None:
    """No watcher when there is nothing on screen to watch: verdict "other"
    (paste skipped), a failed paste, or watcher_enabled=False.

    A COUNTING factory (not a raising one) — the spawn site is guarded by
    try/except in _do_inject, so a raising factory would be swallowed and
    the test would pass vacuously."""
    calls: list = []

    def factory(*a, **k):
        calls.append((a, k))
        return _FakeWatcherThread()

    # (a) foreground verdict "other" -> paste skipped -> no spawn.
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)
    job_q.put(("dictate", object(), 0x1234))
    _run_worker(job_q, ui_q, state, fg_verdict=lambda h: "other",
                watcher_factory=factory)
    assert calls == [], f"no paste happened — nothing to watch: {calls}"
    assert state.state is State.IDLE

    # (b) paste failed (res.ok False) -> no spawn.
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)
    job_q.put(("dictate", object(), 0x1234))
    with mock.patch("src.main.paste_into",
                    return_value=InjectResult(ok=False, clipboard_restored=True,
                                              reason="window_gone")):
        _run_worker(job_q, ui_q, state, fg_verdict=lambda h: "same",
                    watcher_factory=factory)
    assert calls == [], f"paste failed — nothing to watch: {calls}"
    assert state.state is State.IDLE

    # (c) watcher disabled -> no spawn even on a successful paste.
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)
    job_q.put(("dictate", object(), 0x1234))
    with mock.patch("src.main.paste_into",
                    return_value=InjectResult(ok=True, clipboard_restored=True,
                                              reason=None)):
        _run_worker(job_q, ui_q, state, fg_verdict=lambda h: "same",
                    watcher_enabled=False, watcher_factory=factory)
    assert calls == [], f"watcher disabled — must never spawn: {calls}"
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
