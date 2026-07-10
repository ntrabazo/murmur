"""Headless §8.5 verification: History add/trim/ordering, the 200-entry cap,
atomic-write hygiene, and corrupt-line resilience. No tkinter, no clipboard,
no mic, no network — pure file I/O in a temp dir.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_history.py     (plain asserts)
    .\\.venv\\Scripts\\python -m pytest tests\\test_history.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.history import History  # noqa: E402


def _tmp_history(max_entries: int = 200):
    td = tempfile.TemporaryDirectory()
    return td, History(Path(td.name) / "history.jsonl", max_entries=max_entries)


# ---------------------------------------------------------------------- #
# 1. add / entries ordering                                               #
# ---------------------------------------------------------------------- #

def test_add_and_newest_first() -> None:
    td, h = _tmp_history()
    with td:
        assert h.entries() == []  # missing file == empty history, no crash

        h.add("first", "pasted")
        h.add("second", "discarded")
        h.add("third", "no_target")

        ents = h.entries()
        assert [e["text"] for e in ents] == ["third", "second", "first"], \
            "entries() must be newest-first"
        assert [e["outcome"] for e in ents] == ["no_target", "discarded", "pasted"]
        # ts is local ISO-8601 with offset, e.g. 2026-07-04T14:31:07-04:00
        assert "T" in ents[0]["ts"] and len(ents[0]["ts"]) >= 19, ents[0]["ts"]

        # On-disk format: JSONL, oldest first.
        lines = (Path(td.name) / "history.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["text"] == "first"
        assert json.loads(lines[-1])["text"] == "third"


def test_unknown_outcome_recorded_anyway() -> None:
    td, h = _tmp_history()
    with td:
        h.add("odd one", "eaten_by_grue")  # logs a warning, must not drop
        assert h.entries()[0]["outcome"] == "eaten_by_grue"


# ---------------------------------------------------------------------- #
# 2. the 200 cap (trim on save AND on load)                               #
# ---------------------------------------------------------------------- #

def test_cap_trims_oldest_on_add() -> None:
    td, h = _tmp_history(max_entries=200)
    with td:
        for i in range(205):
            h.add(f"entry {i}", "pasted")
        ents = h.entries()
        assert len(ents) == 200, f"cap violated: {len(ents)}"
        assert ents[0]["text"] == "entry 204"    # newest kept
        assert ents[-1]["text"] == "entry 5"     # 0..4 trimmed away


def test_cap_enforced_on_load_of_oversized_file() -> None:
    td, h = _tmp_history(max_entries=200)
    with td:
        # Externally-grown file (e.g. cap lowered later) must heal on load.
        path = Path(td.name) / "history.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for i in range(250):
                f.write(json.dumps({"ts": "x", "text": f"e{i}",
                                    "outcome": "pasted"}) + "\n")
        ents = h.entries()
        assert len(ents) == 200
        assert ents[0]["text"] == "e249" and ents[-1]["text"] == "e50"


# ---------------------------------------------------------------------- #
# 3. atomic write: real file replaced, no .tmp left behind                #
# ---------------------------------------------------------------------- #

def test_atomic_write_leaves_no_tmp() -> None:
    td, h = _tmp_history()
    with td:
        for i in range(10):
            h.add(f"entry {i}", "pasted")
        dirfiles = sorted(p.name for p in Path(td.name).iterdir())
        assert dirfiles == ["history.jsonl"], \
            f"tmp file leaked or target missing: {dirfiles}"


# ---------------------------------------------------------------------- #
# 4. corrupt / malformed lines are skipped, never fatal                   #
# ---------------------------------------------------------------------- #

def test_corrupt_line_skipped() -> None:
    td, h = _tmp_history()
    with td:
        path = Path(td.name) / "history.jsonl"
        path.write_text(
            json.dumps({"ts": "a", "text": "good one", "outcome": "pasted"}) + "\n"
            + "{this is not json at all\n"                      # corrupt
            + json.dumps(["not", "a", "dict"]) + "\n"           # wrong shape
            + json.dumps({"ts": "b", "outcome": "pasted"}) + "\n"  # no "text"
            + "\n"                                              # blank line
            + json.dumps({"ts": "c", "text": "good two", "outcome": "discarded"}) + "\n",
            encoding="utf-8",
        )
        ents = h.entries()
        assert [e["text"] for e in ents] == ["good two", "good one"], ents

        # And adding after a corrupt load rewrites a clean file.
        h.add("good three", "pasted")
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert all(json.loads(ln)["text"].startswith("good") for ln in lines)


# ---------------------------------------------------------------------- #
# 5. lock sanity: concurrent adds from two threads lose nothing           #
# ---------------------------------------------------------------------- #

def test_concurrent_adds() -> None:
    td, h = _tmp_history()
    with td:
        n_per_thread = 25

        def adder(tag: str) -> None:
            for i in range(n_per_thread):
                h.add(f"{tag}-{i}", "pasted")

        threads = [threading.Thread(target=adder, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ents = h.entries()
        assert len(ents) == 2 * n_per_thread, \
            f"lost writes under concurrency: {len(ents)}"


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
