"""Headless Chunk 7 verification: the single-instance mutex actually excludes
a SECOND PROCESS (the thing it exists for), not just a second in-process call.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_single_instance.py     (plain asserts)
    .\\.venv\\Scripts\\python -m pytest tests\\test_single_instance.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.single_instance as si  # noqa: E402

#: Child process: tries the lock, prints the result, exits (its handle —
#: acquired or not — closes with the process).
_CHILD = (
    f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
    "from src.single_instance import acquire_single_instance_lock; "
    "print(acquire_single_instance_lock())"
)


def _child_acquires() -> bool:
    out = subprocess.run(
        [sys.executable, "-c", _CHILD], capture_output=True, text=True,
        timeout=30, check=True,
    )
    return out.stdout.strip() == "True"


def test_mutex_name_is_global() -> None:
    # "Global\\" prefix = kernel global namespace, works across sessions.
    assert si.MUTEX_NAME == "Global\\FlowCloneSingleInstance"


def test_second_process_blocked_then_freed() -> None:
    """acquire here -> a child process must get False; release -> True."""
    assert si.acquire_single_instance_lock() is True, \
        "first acquisition in a clean environment must succeed"
    try:
        assert _child_acquires() is False, \
            "a second process acquired the mutex while we hold it"
    finally:
        # Release: close the module-held handle (normally lives for the
        # process lifetime — tests are the only place this is done by hand).
        si._mutex_handle.Close()
        si._mutex_handle = None

    assert _child_acquires() is True, \
        "after releasing, a fresh process should acquire cleanly"


def test_reacquire_after_release() -> None:
    """The same process can re-acquire after an explicit release."""
    assert si.acquire_single_instance_lock() is True
    si._mutex_handle.Close()
    si._mutex_handle = None


def test_show_window_signal_round_trip() -> None:
    """v2 launch-UX: a listener wakes on a show-window signal and invokes the
    callback. Same-process here (the named event is shared by name), which is
    exactly the cross-process mechanism a Search-launched second instance
    uses via signal_show_window()."""
    import threading

    fired = threading.Event()
    si.start_show_window_listener(fired.set)
    try:
        assert si.signal_show_window() is True
        # The listener runs on a daemon thread; give it a beat to wake.
        assert fired.wait(timeout=5.0), "listener did not fire on the signal"
    finally:
        if si._show_event_handle is not None:
            si._show_event_handle.Close()
            si._show_event_handle = None


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
