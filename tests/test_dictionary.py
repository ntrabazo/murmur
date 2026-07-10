"""Chunk 6 headless verification: Dictionary unit behavior (plan §5) plus
the main.py wiring proof — the worker-level happy path from
test_app_state.py rerun with stub STT/cleaner but a REAL Dictionary on a
temp path.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_dictionary.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_dictionary.py
"""

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
from src.cleanup import CleanResult  # noqa: E402
from src.dictionary import Dictionary  # noqa: E402
from src.stt import SttResult  # noqa: E402


def _tmp(td: str) -> Path:
    return Path(td) / "dictionary.json"


# ---------------------------------------------------------------------- #
# 1. load / add_correction merge / save / reload round-trip               #
# ---------------------------------------------------------------------- #

def test_fresh_load_and_merge_dedupe() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td))
        assert d.entries == []

        # Two corrections for the SAME canonical (case-insensitive match on
        # the second) must merge into ONE entry, not duplicate (plan §5).
        d.add_correction("whisper flow", "Wispr Flow")
        d.add_correction("wisper flow", "wispr flow")  # different case, same entry
        assert len(d.entries) == 1, d.entries
        e = d.entries[0]
        assert e["canonical"] == "Wispr Flow"       # first-seen casing kept
        assert e["misheard"] == ["whisper flow", "wisper flow"]
        assert e["times_corrected"] == 2            # re-confirm bumped
        assert e["source"] == "learned" and e["enabled"] is True

        # Re-learning an EXISTING variant: no duplicate, counter still bumps.
        d.add_correction("Whisper Flow", "Wispr Flow")
        assert d.entries[0]["misheard"] == ["whisper flow", "wisper flow"]
        assert d.entries[0]["times_corrected"] == 3

        # Round-trip: save -> fresh load -> identical entries.
        d.save()
        d2 = Dictionary.load(_tmp(td))
        assert d2.entries == d.entries
        raw = json.loads(_tmp(td).read_text(encoding="utf-8"))
        assert raw["version"] == 1 and raw["updated"] is not None


def test_corrupt_file_quarantined_never_crashes() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = _tmp(td)
        p.write_text("{this is not json", encoding="utf-8")
        d = Dictionary.load(p)                     # must NOT raise
        assert d.entries == []
        bad = list(Path(td).glob("dictionary.json.bad-*"))
        assert len(bad) == 1, f"expected one quarantine file, got {bad}"
        assert bad[0].read_text(encoding="utf-8") == "{this is not json"
        assert not p.exists()                      # moved, not copied
        d.add_correction("data kai", "Theta Chi")  # fresh dict is writable
        d.save()
        assert Dictionary.load(p).entries[0]["canonical"] == "Theta Chi"


# ---------------------------------------------------------------------- #
# 2. Prompt builders: content, ranking, caps, enabled flag                #
# ---------------------------------------------------------------------- #

def _seed(d: Dictionary) -> None:
    d.add_correction("whisper flow", "Wispr Flow")
    d.add_correction("data kai", "Theta Chi")
    d.add_correction("case you", "KSU")


def test_prompt_builders_and_ranking() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td))
        _seed(d)
        # Rank KSU up via applied-in-accepted-output bumps (plan §5 ranking:
        # times_applied first, then last_used).
        assert d.note_applied("I go to KSU.") == 1
        assert d.note_applied("ksu is case-insensitive but word-bounded") == 1
        assert d.note_applied("Theta Chi rush week") == 1

        p = d.stt_prompt()
        assert p.startswith("Glossary: ")
        assert p == "Glossary: KSU, Theta Chi, Wispr Flow", p

        ctx = d.cleanup_context()
        assert ctx.splitlines() == [
            '- "case you" → "KSU"',
            '- "data kai" → "Theta Chi"',
            '- "whisper flow" → "Wispr Flow"',
        ], ctx


def test_entry_cap_and_token_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary(_tmp(td), max_prompt_entries=2)
        _seed(d)
        d.note_applied("KSU")           # KSU outranks the tied others
        assert d.stt_prompt() == "Glossary: KSU, Wispr Flow"
        assert len(d.cleanup_context().splitlines()) == 2

        # Token hard cap: a canonical too big for the ~200-token budget
        # never enters the prompt (Whisper truncates the FRONT on overflow,
        # which would silently drop the highest-ranked names).
        d2 = Dictionary(_tmp(td))
        d2.add_correction("big", "Word" * 300)     # ~300 tokens on its own
        d2.add_correction("whisper flow", "Wispr Flow")
        assert d2.stt_prompt() == "Glossary: Wispr Flow"


def test_enabled_flag_respected_everywhere() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td))
        _seed(d)
        d.entries[0]["enabled"] = False            # kill Wispr Flow
        assert "Wispr Flow" not in d.stt_prompt()
        assert "Wispr Flow" not in d.cleanup_context()
        assert d.lookup_misheard("whisper flow") is None
        # note_applied skips disabled entries too.
        assert d.note_applied("Wispr Flow forever") == 0
        assert d.entries[0]["times_applied"] == 0
        # But add_correction still matches by canonical (history accumulates
        # in one place); it does NOT flip enabled back on.
        d.add_correction("whispr flow", "wispr flow")
        assert len(d.entries) == 3
        assert d.entries[0]["enabled"] is False

        # Empty dictionary edge: both builders return "" (main passes
        # initial_prompt=None in that case).
        empty = Dictionary.load(Path(td) / "other.json")
        assert empty.stt_prompt() == "" and empty.cleanup_context() == ""


def test_autoenable_false_creates_disabled_entries() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td), autoenable=False)
        d.add_correction("whisper flow", "Wispr Flow")
        assert d.entries[0]["enabled"] is False
        assert d.stt_prompt() == ""                # disabled from birth


def test_set_enabled_and_remove() -> None:
    """v2 U4: the app window's per-entry toggle/delete methods —
    case-insensitive canonical lookup like add_correction, missing
    canonical is a False-returning no-op, effects persist across save."""
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td))
        _seed(d)  # Wispr Flow / Theta Chi / KSU

        # set_enabled: case-insensitive, flips the flag, prompts respect it.
        assert d.set_enabled("WISPR FLOW", False) is True
        assert d.entries[0]["enabled"] is False
        assert "Wispr Flow" not in d.stt_prompt()
        assert d.lookup_misheard("whisper flow") is None
        assert d.set_enabled("wispr flow", True) is True  # re-enable
        assert "Wispr Flow" in d.stt_prompt()
        # Missing canonical: no-op, False, nothing changed.
        assert d.set_enabled("no such entry", False) is False
        assert len(d.entries) == 3

        # remove: case-insensitive, drops entry + its misheard index.
        assert d.remove("THETA CHI") is True
        assert len(d.entries) == 2
        assert "Theta Chi" not in d.stt_prompt()
        assert d.lookup_misheard("data kai") is None
        # Already gone: no-op, False.
        assert d.remove("theta chi") is False
        assert len(d.entries) == 2

        # Effects persist through the existing atomic save.
        d.set_enabled("ksu", False)
        d.save()
        on_disk = Dictionary.load(_tmp(td))
        assert {e["canonical"] for e in on_disk.entries} == {"Wispr Flow", "KSU"}
        assert on_disk.stt_prompt() == "Glossary: Wispr Flow"


def test_reload_if_changed_picks_up_hand_edits() -> None:
    """Tray 'Open dictionary' edits take effect next dictation (plan §6)."""
    with tempfile.TemporaryDirectory() as td:
        d = Dictionary.load(_tmp(td))
        d.add_correction("whisper flow", "Wispr Flow")
        d.save()
        assert d.reload_if_changed() is False      # nothing changed

        # Deflake (seen 2026-07-05): if save() and the hand-edit below land
        # within the same st_mtime tick, the mtime check can't see the edit.
        # A real hand-edit is never sub-20ms after a dictation's save.
        import time
        time.sleep(0.02)

        # Hand-edit on disk: disable the entry, add a manual one.
        raw = json.loads(_tmp(td).read_text(encoding="utf-8"))
        raw["entries"][0]["enabled"] = False
        raw["entries"].append({
            "canonical": "AZ-104", "misheard": ["a z one oh four"],
            "source": "manual", "enabled": True, "times_corrected": 0,
            "times_applied": 0, "first_seen": None, "last_used": None,
        })
        _tmp(td).write_text(json.dumps(raw), encoding="utf-8")

        assert d.reload_if_changed() is True
        assert d.stt_prompt() == "Glossary: AZ-104"


# ---------------------------------------------------------------------- #
# 3. main.py wiring proof — worker happy path with a REAL Dictionary      #
# ---------------------------------------------------------------------- #

class _RecordingStt:
    """Stub STT that records the initial_prompt it was given."""
    def __init__(self, text: str) -> None:
        self.text = text
        self.seen_initial_prompt: str | None = "NEVER-CALLED"

    def transcribe(self, audio, initial_prompt=None):
        self.seen_initial_prompt = initial_prompt
        return SttResult(text=self.text, audio_sec=1.0, latency_sec=0.5)


class _RecordingCleaner:
    """Stub cleaner that records the dictionary_block and echoes input."""
    def __init__(self) -> None:
        self.seen_block: str | None = "NEVER-CALLED"

    def clean(self, raw, dictionary_block=""):
        self.seen_block = dictionary_block
        return CleanResult(text=raw, degraded=False, latency_sec=0.1)


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _run_worker(job_q, ui_q, stt, cleaner, state, dictionary,
                fg_verdict=lambda h: "other") -> None:
    """fg_verdict defaults to "other" so the real clipboard/foreground is
    never touched unless a test opts in (v2 U2)."""
    from src.main import pipeline_worker
    t = threading.Thread(
        target=pipeline_worker, args=(job_q, ui_q, stt, cleaner, state),
        kwargs={"dictionary": dictionary, "min_similarity": 0.45,
                "fg_verdict": fg_verdict},
        daemon=True,
    )
    t.start()
    job_q.put(None)
    t.join(timeout=10)
    assert not t.is_alive(), "worker did not exit on sentinel"


def test_worker_autopaste_bumps_applied() -> None:
    """v2 U2 interim form of the old accept-learns test: with the review
    popup gone there is no edit to diff, so the only learning signal this
    window is note_applied on the pasted text — a dictation whose cleaned
    text contains a known canonical must bump times_applied AND persist it,
    with NO 'Learned:' toast (correction learning is offline until U3's
    UIA watcher / U4's teach path reinstate it).

    hwnd 0x1234 is dead, so the paste aborts (no_target) — the bump must
    still run, exactly as the old accept path bumped regardless of paste
    success."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        dictionary.add_correction("whisper flow", "Wispr Flow")
        dictionary.save()

        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()
        assert state.try_transition(State.IDLE, State.RECORDING)
        assert state.try_transition(State.RECORDING, State.TRANSCRIBING)

        # Stub cleaner echoes, so the pasted text IS the stt text — and it
        # contains the canonical.
        job_q.put(("dictate", object(), 0x1234))
        _run_worker(job_q, ui_q, _RecordingStt("talk to Wispr Flow"),
                    _RecordingCleaner(), state, dictionary,
                    fg_verdict=lambda h: "same")

        msgs = _drain(ui_q)
        learned = [m for m in msgs if m[0] == "toast" and m[1].startswith("Learned:")]
        assert learned == [], f"no popup edits exist — nothing to learn: {msgs}"
        assert state.state is State.IDLE

        # Persistence: a FRESH load from the temp path sees the bump.
        on_disk = Dictionary.load(_tmp(td))
        assert len(on_disk.entries) == 1
        e = on_disk.entries[0]
        assert e["canonical"] == "Wispr Flow"
        assert e["times_applied"] == 1 and e["last_used"] is not None


def test_worker_learned_corrections_job() -> None:
    """v2 U3: a ("learned_corrections", [Correction, ...]) job — what the
    UIA watcher posts after seeing the user fix a word in the target app —
    must produce the 'Learned:' toast AND a persisted entry on a fresh
    Dictionary.load. This reinstates the persistence assertions the U2
    migration suspended, via the new path (plan U3 test 3)."""
    from src.diff_learner import Correction

    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()

        job_q.put(("learned_corrections",
                   [Correction("whisper flow", "Wispr Flow")]))
        _run_worker(job_q, ui_q, _RecordingStt("unused"), _RecordingCleaner(),
                    state, dictionary)

        msgs = _drain(ui_q)
        assert ("toast", 'Learned: "whisper flow" → "Wispr Flow"') in msgs, msgs
        assert not any(m[0] == "toast" and "error" in m[1] for m in msgs), msgs
        assert state.state is State.IDLE  # never left it — no dictation ran

        # Persistence: the worker saved; a FRESH load sees the entry.
        on_disk = Dictionary.load(_tmp(td))
        assert len(on_disk.entries) == 1, on_disk.entries
        e = on_disk.entries[0]
        assert e["canonical"] == "Wispr Flow"
        assert e["misheard"] == ["whisper flow"]
        assert e["source"] == "learned" and e["times_corrected"] == 1


def test_worker_dictate_passes_both_prompt_layers() -> None:
    """_handle_dictate must feed stt_prompt() into stt.transcribe and
    cleanup_context() into cleaner.clean (the two Chunk-6 hooks). v2: the
    job now flows straight through to the inject step — fg_verdict "other"
    (the _run_worker default) keeps the real clipboard untouched, and no
    ("review",...) message may ever appear."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        dictionary.add_correction("whisper flow", "Wispr Flow")
        dictionary.save()

        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()
        assert state.try_transition(State.IDLE, State.RECORDING)
        assert state.try_transition(State.RECORDING, State.TRANSCRIBING)
        stt = _RecordingStt("hello there")
        cleaner = _RecordingCleaner()
        job_q.put(("dictate", object(), 0x42))

        _run_worker(job_q, ui_q, stt, cleaner, state, dictionary)

        assert stt.seen_initial_prompt == "Glossary: Wispr Flow"
        assert cleaner.seen_block == '- "whisper flow" → "Wispr Flow"'
        msgs = _drain(ui_q)
        assert not any(m[0] == "review" for m in msgs), \
            f"the review message must be dead in v2: {msgs}"
        assert state.state is State.IDLE


def test_worker_dictate_empty_dictionary_passes_none_prompt() -> None:
    """An empty dictionary must yield initial_prompt=None (not ''), and an
    empty dictionary_block — unbiased dictation."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()
        state.try_transition(State.IDLE, State.RECORDING)
        state.try_transition(State.RECORDING, State.TRANSCRIBING)
        stt = _RecordingStt("hi")
        cleaner = _RecordingCleaner()
        job_q.put(("dictate", object(), 0))

        _run_worker(job_q, ui_q, stt, cleaner, state, dictionary,
                    fg_verdict=lambda h: "other")

        assert stt.seen_initial_prompt is None
        assert cleaner.seen_block == ""


# ---------------------------------------------------------------------- #
# 4. v2 U4 app-window job kinds — teach / dict_toggle / dict_delete       #
# ---------------------------------------------------------------------- #

def test_worker_teach_job_learns_and_snapshots() -> None:
    """The app window's "Save & teach": ("teach", original, edited) runs the
    same learner on the worker, adds the correction, toasts 'Learned:', and
    always ends with a ("dict_changed", snapshot) for the Dictionary tab."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()

        job_q.put(("teach", "talk to whisper flow", "talk to Wispr Flow"))
        _run_worker(job_q, ui_q, _RecordingStt("unused"), _RecordingCleaner(),
                    state, dictionary)

        msgs = _drain(ui_q)
        assert ("toast", 'Learned: "whisper flow" → "Wispr Flow"') in msgs, msgs
        assert any(m[0] == "dict_changed" for m in msgs), msgs
        assert not any(m[0] == "toast" and "error" in m[1] for m in msgs), msgs

        on_disk = Dictionary.load(_tmp(td))
        assert len(on_disk.entries) == 1
        assert on_disk.entries[0]["canonical"] == "Wispr Flow"


def test_worker_teach_job_no_corrections_toasts_and_snapshots() -> None:
    """A teach edit with no learnable correction toasts the honest 'none'
    message and still posts a snapshot — never an error."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        job_q, ui_q = queue.Queue(), queue.Queue()
        state = AppState()

        # A full rewrite — the learner's global-rewrite guard learns nothing.
        job_q.put(("teach", "the meeting is at noon", "let's grab lunch instead"))
        _run_worker(job_q, ui_q, _RecordingStt("unused"), _RecordingCleaner(),
                    state, dictionary)

        msgs = _drain(ui_q)
        assert ("toast", "no corrections found in that edit") in msgs, msgs
        assert any(m[0] == "dict_changed" for m in msgs), msgs
        assert Dictionary.load(_tmp(td)).entries == []


def test_worker_dict_toggle_and_delete_jobs() -> None:
    """dict_toggle flips enabled + persists; dict_delete removes + persists;
    both post a dict_changed snapshot; a missing canonical is a no-op that
    still reconverges the UI via the snapshot."""
    with tempfile.TemporaryDirectory() as td:
        dictionary = Dictionary.load(_tmp(td))
        dictionary.add_correction("data kai", "Theta Chi")
        dictionary.save()

        # Disable it.
        job_q, ui_q = queue.Queue(), queue.Queue()
        job_q.put(("dict_toggle", "Theta Chi", False))
        _run_worker(job_q, ui_q, _RecordingStt("x"), _RecordingCleaner(),
                    AppState(), dictionary)
        assert any(m[0] == "dict_changed" for m in _drain(ui_q))
        assert Dictionary.load(_tmp(td)).entries[0]["enabled"] is False

        # Missing-canonical toggle: no-op, still snapshots, no error.
        job_q, ui_q = queue.Queue(), queue.Queue()
        job_q.put(("dict_toggle", "Nonexistent", True))
        _run_worker(job_q, ui_q, _RecordingStt("x"), _RecordingCleaner(),
                    AppState(), dictionary)
        msgs = _drain(ui_q)
        assert any(m[0] == "dict_changed" for m in msgs)
        assert not any(m[0] == "toast" and "error" in m[1] for m in msgs)

        # Delete it.
        job_q, ui_q = queue.Queue(), queue.Queue()
        job_q.put(("dict_delete", "Theta Chi"))
        _run_worker(job_q, ui_q, _RecordingStt("x"), _RecordingCleaner(),
                    AppState(), dictionary)
        assert any(m[0] == "dict_changed" for m in _drain(ui_q))
        assert Dictionary.load(_tmp(td)).entries == []


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
