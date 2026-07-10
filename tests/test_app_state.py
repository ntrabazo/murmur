"""Headless Chunk 4 verification: state-machine CAS logic + pipeline_worker
queue plumbing, with NO tkinter mainloop, NO hotkey listener, NO mic, NO
model, NO network.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_app_state.py     (plain asserts)
    .\\.venv\\Scripts\\python -m pytest tests\\test_app_state.py   (if pytest ever installed)
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
from src.cleanup import CleanResult  # noqa: E402
from src.stt import SttResult  # noqa: E402


# ---------------------------------------------------------------------- #
# 1. CAS semantics                                                        #
# ---------------------------------------------------------------------- #

def test_cas_basics() -> None:
    s = AppState()
    assert s.state is State.IDLE

    # Winning transition.
    assert s.try_transition(State.IDLE, State.RECORDING) is True
    assert s.state is State.RECORDING

    # Re-entrancy guard: same transition again must be refused, state unchanged.
    assert s.try_transition(State.IDLE, State.RECORDING) is False
    assert s.state is State.RECORDING

    # Wrong-source transition refused.
    assert s.try_transition(State.CLEANING, State.INJECTING) is False
    assert s.state is State.RECORDING

    # Tuple-of-sources form.
    assert s.try_transition((State.IDLE, State.RECORDING), State.TRANSCRIBING) is True
    assert s.state is State.TRANSCRIBING

    # force() resets from anywhere (the worker exception path).
    s.force(State.IDLE)
    assert s.state is State.IDLE


def test_full_cycle() -> None:
    s = AppState()
    path = [State.RECORDING, State.TRANSCRIBING, State.CLEANING,
            State.INJECTING, State.IDLE]
    cur = State.IDLE
    for nxt in path:
        assert s.try_transition(cur, nxt) is True, f"{cur} -> {nxt} refused"
        cur = nxt
    assert s.state is State.IDLE


# ---------------------------------------------------------------------- #
# 2. Thread race: exactly ONE of N concurrent pressers may win IDLE->RECORDING
# ---------------------------------------------------------------------- #

def test_cas_race() -> None:
    s = AppState()
    n = 16
    barrier = threading.Barrier(n)
    results: list[bool] = []
    lock = threading.Lock()

    def presser() -> None:
        barrier.wait()  # maximize contention
        won = s.try_transition(State.IDLE, State.RECORDING)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=presser) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"expected exactly 1 winner, got {sum(results)}"
    assert s.state is State.RECORDING


# ---------------------------------------------------------------------- #
# 3. pipeline_worker smoke: real queues + real state machine, stub STT/Cleaner
# ---------------------------------------------------------------------- #

class _StubStt:
    def __init__(self, text: str = "hello world", raise_exc: bool = False) -> None:
        self.text = text
        self.raise_exc = raise_exc

    def transcribe(self, audio, initial_prompt=None):
        if self.raise_exc:
            raise RuntimeError("boom (simulated STT crash)")
        return SttResult(text=self.text, audio_sec=1.0, latency_sec=0.5)


class _StubCleaner:
    def __init__(self, degraded: bool = False) -> None:
        self.degraded = degraded

    def clean(self, raw, dictionary_block=""):
        if self.degraded:
            return CleanResult(text=raw, degraded=True, latency_sec=0.0,
                               error="offline: stub")
        return CleanResult(text=raw.capitalize() + ".", degraded=False,
                           latency_sec=0.3)


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _run_worker_until_sentinel(job_q, ui_q, stt, cleaner, state,
                               history=None, **kwargs) -> None:
    """kwargs pass through to pipeline_worker — tests use it to inject a
    fake fg_verdict so the suite never touches the real foreground window
    or clipboard (v2 U2)."""
    from src.main import pipeline_worker
    t = threading.Thread(
        target=pipeline_worker, args=(job_q, ui_q, stt, cleaner, state),
        kwargs={"history": history, **kwargs},
        daemon=True,
    )
    t.start()
    job_q.put(None)  # sentinel already queued behind the real jobs
    t.join(timeout=10)
    assert not t.is_alive(), "worker did not exit on sentinel"


def test_worker_autopaste_happy() -> None:
    """v2 U2: a dictate job flows straight through to the inject step — no
    ("review",...) message ever. fg_verdict says "same" so the paste
    proceeds; hwnd 0x1234 is dead, so paste_into aborts window_gone BEFORE
    any clipboard write (headless-safe), the transcript is recorded to
    History with outcome "no_target", and the toast points at History.

    (Absorbs the retired test_worker_inject_window_gone — the standalone
    "inject" job kind is gone; its assertions live here now.)
    """
    import tempfile
    from src.history import History

    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    # Simulate on_press + on_release having run:
    assert state.try_transition(State.IDLE, State.RECORDING)
    assert state.try_transition(State.RECORDING, State.TRANSCRIBING)

    with tempfile.TemporaryDirectory() as td:
        hist = History(Path(td) / "history.jsonl")
        job_q.put(("dictate", object(), 0x1234))  # audio is opaque to the stubs

        _run_worker_until_sentinel(job_q, ui_q, _StubStt(), _StubCleaner(),
                                   state, history=hist,
                                   fg_verdict=lambda h: "same")

        msgs = _drain(ui_q)
        assert not any(m[0] == "review" for m in msgs), \
            f"the review message must be dead in v2: {msgs}"
        assert any(m[0] == "toast" and "History" in m[1] for m in msgs), msgs
        assert state.state is State.IDLE, "auto-paste must land back in IDLE"

        ents = hist.entries()
        assert len(ents) == 1, ents
        assert ents[0]["text"] == "Hello world."
        assert ents[0]["outcome"] == "no_target", ents[0]


def test_worker_foreground_changed() -> None:
    """v2 U2 stale-window branch: fg_verdict "other" (user switched apps
    mid-pipeline) -> NO paste at all (real-clipboard safety: paste_into is
    patched to explode if touched), History outcome "window_changed", the
    exact toast, and back to IDLE."""
    import tempfile
    from unittest import mock
    from src.history import History

    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    assert state.try_transition(State.IDLE, State.RECORDING)
    assert state.try_transition(State.RECORDING, State.TRANSCRIBING)

    verdict_calls: list[int] = []

    def fake_verdict(hwnd: int) -> str:
        verdict_calls.append(hwnd)
        return "other"

    with tempfile.TemporaryDirectory() as td:
        hist = History(Path(td) / "history.jsonl")
        job_q.put(("dictate", object(), 0x1234))

        with mock.patch("src.main.paste_into",
                        side_effect=AssertionError("paste_into must NOT run "
                                                   "on verdict 'other'")):
            _run_worker_until_sentinel(job_q, ui_q, _StubStt(), _StubCleaner(),
                                       state, history=hist,
                                       fg_verdict=fake_verdict)

        msgs = _drain(ui_q)
        assert verdict_calls == [0x1234], verdict_calls
        assert ("toast", "window changed — saved to History") in msgs, msgs
        assert not any(m[0] == "toast" and "error" in m[1] for m in msgs), \
            f"patched paste_into must never have been reached: {msgs}"
        assert state.state is State.IDLE

        ents = hist.entries()
        assert len(ents) == 1, ents
        assert ents[0]["text"] == "Hello world."
        assert ents[0]["outcome"] == "window_changed", ents[0]


def test_worker_degraded_toasts_and_pastes() -> None:
    """v2: degraded cleanup (Claude unreachable) still pastes — the raw/
    regex-fallback text is usable. A "raw" toast is the visibility (the old
    popup meta badge is gone), and the pipeline still reaches the inject
    step (History records the outcome)."""
    import tempfile
    from src.history import History

    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)

    with tempfile.TemporaryDirectory() as td:
        hist = History(Path(td) / "history.jsonl")
        job_q.put(("dictate", object(), 42))

        _run_worker_until_sentinel(job_q, ui_q, _StubStt(),
                                   _StubCleaner(degraded=True), state,
                                   history=hist, fg_verdict=lambda h: "same")

        msgs = _drain(ui_q)
        assert any(m[0] == "toast" and "raw" in m[1] for m in msgs), msgs
        assert len(hist.entries()) == 1, "degraded text must still reach inject"
        assert state.state is State.IDLE


def test_worker_heard_nothing() -> None:
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)
    job_q.put(("dictate", object(), 0))

    _run_worker_until_sentinel(job_q, ui_q, _StubStt(text=""), _StubCleaner(), state)

    msgs = _drain(ui_q)
    assert ("toast", "heard nothing") in msgs, msgs
    assert not any(m[0] == "review" for m in msgs), "no popup on silence"
    assert state.state is State.IDLE


def test_worker_exception_resets_state() -> None:
    job_q, ui_q = queue.Queue(), queue.Queue()
    state = AppState()
    state.try_transition(State.IDLE, State.RECORDING)
    state.try_transition(State.RECORDING, State.TRANSCRIBING)
    job_q.put(("dictate", object(), 0))

    _run_worker_until_sentinel(
        job_q, ui_q, _StubStt(raise_exc=True), _StubCleaner(), state
    )

    msgs = _drain(ui_q)
    assert ("toast", "error — see log") in msgs, msgs
    assert state.state is State.IDLE, "exception must force-reset to IDLE"


# ---------------------------------------------------------------------- #
# Plain-python runner                                                     #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)  # keep test output clean
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
