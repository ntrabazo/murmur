"""v2 U4 headless smoke test for the customtkinter app window.

Constructs the real AppWindow against a withdrawn CTk root, drives its full
public API (show / refresh_dictionary / refresh_if_visible / set_paused /
hide / destroy), and asserts the per-entry actions post the correct
single-writer job tuples to the worker. No worker, no mic, no network.

Skips cleanly if a Tk display can't be created (e.g. a truly headless CI
box); on Windows with a desktop session it runs for real.

    .\\.venv\\Scripts\\python -m pytest tests\\test_app_window.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _StubHistory:
    def entries(self) -> list[dict]:
        return [
            {"ts": "2026-07-06T10:00:00-04:00",
             "text": "talk to whisper flow", "outcome": "pasted"},
            {"ts": "2026-07-06T09:00:00-04:00",
             "text": "some other note", "outcome": "window_changed"},
        ]


_DICT_SNAPSHOT = [{
    "canonical": "Wispr Flow", "misheard": ["whisper flow"],
    "source": "learned", "enabled": True,
    "times_corrected": 2, "times_applied": 5,
    "first_seen": None, "last_used": None,
}]


def _make_window():
    """Build a CTk root + AppWindow, or skip if no display is available."""
    try:
        import customtkinter as ctk
        from src.app_window import AppWindow
    except Exception as e:  # pragma: no cover - import/display failure
        pytest.skip(f"customtkinter/app_window unavailable: {e}")
    try:
        root = ctk.CTk()
    except Exception as e:  # pragma: no cover - no display
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    jobs: list[tuple] = []
    cfg = {"model_size": "base.en", "hotkey": "ctrl_r"}
    win = AppWindow(root, _StubHistory(), jobs.append, cfg)
    return root, win, jobs


def test_app_window_constructs_and_drives_api() -> None:
    root, win, _jobs = _make_window()
    try:
        win.show()
        root.update()
        win.refresh_dictionary(_DICT_SNAPSHOT)
        root.update()
        win.refresh_if_visible()
        root.update()
        win.set_paused(True)
        root.update()
        win.set_paused(False)
        root.update()
        win.hide()
        root.update()
    finally:
        win.destroy()
        root.destroy()


def test_teach_action_posts_single_writer_job() -> None:
    root, win, jobs = _make_window()
    try:
        win.show()
        root.update()
        # Select the first transcript and simulate an in-box edit.
        win._selected = 0
        win._detail_box.delete("1.0", "end")
        win._detail_box.insert("1.0", "talk to Wispr Flow")
        win._teach_selected()
        assert ("teach", "talk to whisper flow", "talk to Wispr Flow") in jobs, jobs
    finally:
        win.destroy()
        root.destroy()


def test_dict_toggle_and_delete_post_jobs() -> None:
    root, win, jobs = _make_window()
    try:
        win.show()
        win.refresh_dictionary(_DICT_SNAPSHOT)
        root.update()
        win._on_delete("Wispr Flow")
        assert ("dict_delete", "Wispr Flow") in jobs, jobs
        # Toggle posts a 3-tuple ("dict_toggle", canonical, enabled_bool).
        win._on_toggle("Wispr Flow")
        toggles = [j for j in jobs if j and j[0] == "dict_toggle"]
        assert toggles and toggles[0][1] == "Wispr Flow"
        assert isinstance(toggles[0][2], bool)
    finally:
        win.destroy()
        root.destroy()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
