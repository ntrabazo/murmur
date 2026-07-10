"""Preflight startup checks for flow-clone (Chunk 7, plan §6).

Run BEFORE anything else in main(): these turn the two classic
mysteriously-dead-tray-icon states (silent double launch, missing API key)
into a blocking error dialog with the exact fix.

Check order is the spec's, and the FIRST failure short-circuits:

    1. single-instance lock (named Global mutex)
    2. ANTHROPIC_API_KEY present in ~\\.env
    3. a default audio input device exists
    4. STT model resolvable — cheap check only: if the model is already in
       the local cache dir we know it's fine; if not, this is a first run
       and the download needs internet — we deliberately DON'T block on a
       network probe here (plan: the model-load failure path in main()
       already handles offline with its own fatal toast). Check 4 therefore
       never fails startup; it just logs which path we're on.

Note on check 1 living here: acquiring the mutex is the check — the handle
stays alive in single_instance's module global for the process lifetime, so
running the checks IS taking the lock. Call run_startup_checks() exactly
once, from main().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import sounddevice as sd

from src.config import MissingKeyError, get_anthropic_key
from src.single_instance import acquire_single_instance_lock

log = logging.getLogger(__name__)


@dataclass
class StartupCheckResult:
    ok: bool
    fatal_message: str | None = None
    #: True only on the single-instance-lock failure. main() treats this
    #: specially: instead of a fatal dialog, it signals the running instance
    #: to show its window and exits silently (v2 launch-UX).
    already_running: bool = False


def _model_cached(config: dict) -> bool:
    """True if the configured model already exists in the local cache dir.

    faster-whisper's download_root layout is HuggingFace-style:
    <cache>/models--Systran--faster-whisper-<size>/ — a simple name match
    on subdirectories is the cheap check the plan asks for.
    """
    cache_dir = Path(__file__).resolve().parent.parent / config["model_cache_dir"]
    if not cache_dir.is_dir():
        return False
    model_size = str(config["model_size"])
    for entry in cache_dir.iterdir():
        if entry.is_dir() and model_size in entry.name:
            return True
    return False


def run_startup_checks(config: dict) -> StartupCheckResult:
    """Run the four preflight checks in spec order; first failure wins."""
    # (1) single instance — a second copy must die with a clear message, not
    # fight the first over the hotkey hook / mic / tray.
    if not acquire_single_instance_lock():
        return StartupCheckResult(
            ok=False,
            fatal_message="Another flow-clone is already running (check the tray).",
            already_running=True,
        )

    # (2) API key — checked here (presence) so the failure is a dialog, not
    # a stderr print nobody sees under pythonw. main() re-reads the value.
    try:
        get_anthropic_key()
    except MissingKeyError:
        return StartupCheckResult(
            ok=False,
            fatal_message=(
                f"No ANTHROPIC_API_KEY in {Path.home() / '.env'} — add it and restart."
            ),
        )

    # (3) default input device — sd.query_devices(kind="input") raises when
    # there is no default input device at all.
    try:
        sd.query_devices(kind="input")
    except Exception:
        return StartupCheckResult(
            ok=False,
            fatal_message=(
                "No microphone input device found — connect one "
                "(Settings > System > Sound > Input) and restart."
            ),
        )

    # (4) model resolvable — cheap cache-presence check only; never fatal
    # (see module docstring).
    if _model_cached(config):
        log.info("startup checks: model %r found in local cache", config["model_size"])
    else:
        log.info(
            "startup checks: model %r not cached — first run will download "
            "(~%s, needs internet once; offline failure is handled at model load)",
            config["model_size"], "hundreds of MB",
        )

    return StartupCheckResult(ok=True, fatal_message=None)
