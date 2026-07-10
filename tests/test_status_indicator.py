"""v2 U5 headless tests: the redesigned status pill + the recorder level feed.

Drives the real StatusIndicator against a withdrawn Tk root through every
state, forces animation frames, and asserts the level provider actually moves
the bars. Also unit-tests the recorder's EMA level update in isolation (no
PortAudio — call _cb directly with synthetic chunks). Skips cleanly if a Tk
display can't be created.

    .\\.venv\\Scripts\\python -m pytest tests\\test_status_indicator.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import State  # noqa: E402
from src.recorder import Recorder  # noqa: E402


def _make_indicator(level=0.0):
    try:
        import tkinter as tk
        from src.status_indicator import StatusIndicator
    except Exception as e:  # pragma: no cover
        pytest.skip(f"tkinter/status_indicator unavailable: {e}")
    try:
        root = tk.Tk()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    box = {"level": level}
    ind = StatusIndicator(root, level_provider=lambda: box["level"])
    return root, ind, box


def test_pill_drives_through_states_without_error() -> None:
    root, ind, _ = _make_indicator(level=0.5)
    try:
        for st in (State.RECORDING, State.TRANSCRIBING, State.CLEANING,
                   State.IDLE, State.INJECTING, State.RECORDING):
            ind.set_status(st)
            root.update()
        ind.hide()
        root.update()
    finally:
        root.destroy()


def test_recording_bars_respond_to_level() -> None:
    """A higher mic level must produce taller bars than a near-silent one."""
    root, ind, box = _make_indicator(level=0.0)
    try:
        ind.set_status(State.RECORDING)
        box["level"] = 0.02
        quiet = ind._bar_heights()
        box["level"] = 0.95
        loud = ind._bar_heights()
        # Center bar is the tallest; loud must clearly exceed quiet there.
        mid = len(quiet) // 2
        assert loud[mid] > quiet[mid] + 5, (quiet[mid], loud[mid])
    finally:
        root.destroy()


def test_process_mode_ignores_level() -> None:
    """Processing animation is input-independent (breathing), not mic-driven."""
    root, ind, box = _make_indicator(level=0.0)
    try:
        ind.set_status(State.TRANSCRIBING)
        box["level"] = 0.9
        heights = ind._bar_heights()
        # All bars equal in process mode (uniform breathing).
        assert max(heights) - min(heights) < 1e-6, heights
    finally:
        root.destroy()


def test_recorder_level_ema_rises_and_decays() -> None:
    """recorder.level tracks input RMS with EMA smoothing: loud chunks push
    it up, silence lets it fall back toward 0 — no PortAudio needed."""
    rec = Recorder(sample_rate=16000, max_sec=90)
    assert rec.level == 0.0

    loud = np.full((480, 1), 0.3, dtype=np.float32)  # ~0.3 RMS
    for _ in range(20):
        rec._cb(loud, 480, None, None)
    after_loud = rec.level
    assert after_loud > 0.3, after_loud  # gained up toward the ceiling

    silence = np.zeros((480, 1), dtype=np.float32)
    for _ in range(50):
        rec._cb(silence, 480, None, None)
    assert rec.level < after_loud
    assert rec.level < 0.05  # decayed back toward quiet


def test_recorder_level_updates_even_when_disarmed() -> None:
    """The meter must react the instant sound arrives, before arm() — the
    level update happens ahead of the armed check in _cb."""
    rec = Recorder(sample_rate=16000, max_sec=90)
    assert not rec._armed.is_set()
    loud = np.full((480, 1), 0.25, dtype=np.float32)
    for _ in range(10):
        rec._cb(loud, 480, None, None)
    assert rec.level > 0.1
    # Disarmed: no audio was buffered despite the level moving.
    assert rec._frames == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
