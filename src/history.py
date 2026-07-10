"""Transcript history for flow-clone (plan §8.5, outcomes amended v2 U2).

Every dictation that produces text is recorded here with its outcome. This
file REPLACES the killed "consolation clipboard" behavior as the safety
net: if a paste had no target (window gone, focus failure) or was skipped
(user switched apps mid-pipeline), the clipboard is left alone and the
text lives in data/history.jsonl instead — the History viewer (tray ->
History) copies it back out on demand.

Storage: JSON Lines, one entry per line, oldest first on disk:

    {"ts": "2026-07-04T14:31:07-04:00", "text": "...", "outcome": "pasted"}

Outcomes:
    pasted          Ctrl+V delivered to the target window
    no_target       the target window was gone / unfocusable
    paste_failed    the clipboard write for the payload failed
    window_changed  v2 auto-paste: the user moved to a DIFFERENT app while
                    the pipeline was busy — paste deliberately skipped
                    (no focus yank), text saved here instead
    discarded       v1 popup Esc. No code path produces this since the
                    review popup was removed (v2 U2); the label stays in
                    the vocabulary because old history.jsonl lines carry
                    it and U4's app window will reuse it.

Durability (same pattern as the plan's dictionary.json, §5):
- capped at the last 200 entries (trimmed on every save and on load);
- every write is atomic: full rewrite to history.jsonl.tmp then
  os.replace() (atomic on NTFS), so a crash mid-write can't corrupt it;
- a corrupt line on load is logged and skipped, never fatal.

Concurrency: add() runs on the pipeline worker thread (all v2 outcomes);
entries() runs on the tk main thread when the viewer opens. One
threading.Lock serializes all file access (§8.5 wiring note).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: The valid outcome vocabulary (§8.5). An unknown outcome is logged and
#: recorded anyway — history must never drop a transcript over a label.
OUTCOMES = frozenset({"pasted", "no_target", "paste_failed",
                      "window_changed", "discarded"})

_MAX_ENTRIES_DEFAULT = 200


class History:
    """Append-mostly transcript log over a capped JSONL file."""

    def __init__(self, path: Path, max_entries: int = _MAX_ENTRIES_DEFAULT) -> None:
        self._path = Path(path)
        self._max = max_entries
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def add(self, text: str, outcome: str) -> None:
        """Record one transcript with its outcome (local-time ISO-8601 ts).

        Loads, appends, trims to the last `max_entries`, and atomically
        rewrites the file — O(200 tiny lines), negligible next to the
        STT/API latencies on the same worker thread.
        """
        if outcome not in OUTCOMES:
            log.warning("history: unknown outcome %r (recording anyway)", outcome)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "text": text,
            "outcome": outcome,
        }
        with self._lock:
            entries = self._load_unlocked()
            entries.append(entry)
            del entries[:-self._max]
            self._save_unlocked(entries)
        log.info("history: recorded %r outcome (%d chars)", outcome, len(text))

    def entries(self) -> list[dict]:
        """All entries, NEWEST FIRST (what the viewer displays)."""
        with self._lock:
            return list(reversed(self._load_unlocked()))

    # ------------------------------------------------------------------ #
    # File I/O (call with self._lock held)                                #
    # ------------------------------------------------------------------ #

    def _load_unlocked(self) -> list[dict]:
        """Read the JSONL file, oldest first. Corrupt lines are skipped
        (logged), a missing file is an empty history — never fatal."""
        if not self._path.exists():
            return []
        entries: list[dict] = []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("history: could not read %s: %s", self._path, e)
            return []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("history: skipping corrupt line %d: %s", lineno, e)
                continue
            if not isinstance(entry, dict) or "text" not in entry:
                log.warning("history: skipping malformed line %d", lineno)
                continue
            entries.append(entry)
        # Enforce the cap on load too, so an externally-grown file heals.
        del entries[:-self._max]
        return entries

    def _save_unlocked(self, entries: list[dict]) -> None:
        """Atomic full rewrite: tmp file + os.replace (NTFS-atomic)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        lines = "".join(
            json.dumps(e, ensure_ascii=False) + "\n" for e in entries
        )
        try:
            tmp.write_text(lines, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            log.exception("history: save failed (entries kept in memory only "
                          "for this call)")
