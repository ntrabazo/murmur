"""Headless Chunk 7 verification: startup-check message selection and
short-circuit ORDER. The real environment passes all four checks on this
machine (proved separately in the live run_startup_checks() call during
verification) — these tests prove each failure path by stubbing the three
externally-dependent probes, without touching the real mutex, .env, or
audio devices.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_startup_check.py     (plain asserts)
    .\\.venv\\Scripts\\python -m pytest tests\\test_startup_check.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.startup_check as sc  # noqa: E402
from src.config import MissingKeyError  # noqa: E402
from src.startup_check import StartupCheckResult, run_startup_checks  # noqa: E402

_CONFIG = {"model_cache_dir": "data/models", "model_size": "base.en"}


@contextlib.contextmanager
def _stubbed(lock_ok=True, key_ok=True, device_ok=True):
    """Swap startup_check's three probes for stubs; always restore.

    Each stub also records that it RAN, so the short-circuit tests can
    assert later checks were never reached.
    """
    ran: list[str] = []

    def fake_lock() -> bool:
        ran.append("lock")
        return lock_ok

    def fake_key() -> str:
        ran.append("key")
        if not key_ok:
            raise MissingKeyError("stub: no key")
        return "sk-ant-stub"

    class FakeSd:
        @staticmethod
        def query_devices(kind=None):
            ran.append("device")
            if not device_ok:
                raise Exception("stub: no default input device")
            return {"name": "stub mic"}

    orig = (sc.acquire_single_instance_lock, sc.get_anthropic_key, sc.sd)
    sc.acquire_single_instance_lock, sc.get_anthropic_key, sc.sd = (
        fake_lock, fake_key, FakeSd()
    )
    try:
        yield ran
    finally:
        sc.acquire_single_instance_lock, sc.get_anthropic_key, sc.sd = orig


# ---------------------------------------------------------------------- #
# 1. message selection — one exact string per failure                     #
# ---------------------------------------------------------------------- #

def test_second_instance_message() -> None:
    with _stubbed(lock_ok=False):
        res = run_startup_checks(_CONFIG)
    assert res == StartupCheckResult(
        ok=False,
        fatal_message="Another flow-clone is already running (check the tray).",
        already_running=True,
    )
    # already_running is the flag main() keys on to signal-show + silent-exit
    # instead of showing the fatal dialog (v2 launch-UX).
    assert res.already_running is True


def test_missing_key_message() -> None:
    with _stubbed(key_ok=False):
        res = run_startup_checks(_CONFIG)
    assert res.ok is False
    assert res.fatal_message == (
        f"No ANTHROPIC_API_KEY in {Path.home() / '.env'} — add it and restart."
    )


def test_no_input_device_message() -> None:
    with _stubbed(device_ok=False):
        res = run_startup_checks(_CONFIG)
    assert res.ok is False
    assert res.fatal_message == (
        "No microphone input device found — connect one "
        "(Settings > System > Sound > Input) and restart."
    )


def test_all_pass() -> None:
    with _stubbed():
        res = run_startup_checks(_CONFIG)
    assert res == StartupCheckResult(ok=True, fatal_message=None)


# ---------------------------------------------------------------------- #
# 2. short-circuit order — first failure wins, later probes never run     #
# ---------------------------------------------------------------------- #

def test_lock_failure_short_circuits_key_and_device() -> None:
    with _stubbed(lock_ok=False, key_ok=False, device_ok=False) as ran:
        run_startup_checks(_CONFIG)
    assert ran == ["lock"], f"expected only the lock probe to run, got {ran}"


def test_key_failure_short_circuits_device() -> None:
    with _stubbed(key_ok=False, device_ok=False) as ran:
        run_startup_checks(_CONFIG)
    assert ran == ["lock", "key"], ran


def test_pass_runs_all_probes_in_spec_order() -> None:
    with _stubbed() as ran:
        run_startup_checks(_CONFIG)
    assert ran == ["lock", "key", "device"], ran


# ---------------------------------------------------------------------- #
# 3. check 4 is never fatal — model cached or not, startup proceeds       #
# ---------------------------------------------------------------------- #

def test_model_not_cached_does_not_fail_startup() -> None:
    cfg = dict(_CONFIG, model_size="definitely-not-a-cached-model")
    with _stubbed():
        res = run_startup_checks(cfg)
    assert res.ok is True  # first-run download deferred to model load (plan §6)


def test_model_cached_detection_against_real_cache() -> None:
    # This machine has base.en + small.en in data/models from Chunk 2.
    assert sc._model_cached({"model_cache_dir": "data/models",
                             "model_size": "base.en"}) is True
    assert sc._model_cached({"model_cache_dir": "data/models",
                             "model_size": "no-such-model"}) is False
    assert sc._model_cached({"model_cache_dir": "data/no-such-dir",
                             "model_size": "base.en"}) is False


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
