"""Self-correcting dictionary for flow-clone (Chunk 6, plan §5).

data/dictionary.json is the product's standout feature: every entry maps
misheard STT variants to the canonical form Nicolas actually corrected to,
and feeds BOTH layers of the next dictation:

  - stt_prompt()      -> Whisper's initial_prompt ("Glossary: Wispr Flow, ...")
                         biases the decoder toward known proper nouns;
  - cleanup_context() -> the Claude system prompt's misheard->correct block
                         catches whatever decoder biasing missed.

Two independent layers — the pair passing is the feature working (plan §6).

Schema (plan §5, verbatim fields):
    version, updated, entries[]
    entry: canonical (str, exact case), misheard (list[str], lowercased,
    deduped), source ("learned"|"manual"), enabled (bool),
    times_corrected (int, diff-learner re-confirmations),
    times_applied (int, canonical seen in accepted output),
    first_seen / last_used (ISO-8601 | null).

Durability (plan §5):
- load(): JSONDecodeError -> quarantine the corrupt file to
  dictionary.json.bad-<timestamp>, start fresh, NEVER crash.
- save(): write dictionary.json.tmp then os.replace() (atomic on NTFS).
- reload_if_changed(): cheap mtime stat at the start of each dictate job,
  so tray "Open dictionary" hand-edits take effect on the next dictation
  without a restart (plan §6 Chunk 6 failure modes).

The `enabled` flag is respected EVERYWHERE it matters: disabled entries are
excluded from both prompt builders, from times_applied bumping, and from
lookup_misheard(). add_correction() deliberately still matches a disabled
entry by canonical — history accumulates in one place instead of spawning a
duplicate — but the entry stays out of prompts until Nicolas re-enables it
(disabling is his explicit kill switch, plan §5; the learner never
overrides it). v2 U4: the app window's Dictionary tab drives
set_enabled()/remove() per entry — via worker jobs, never directly.

Concurrency: all mutation happens on the single pipeline worker thread
(plan §3.5 — one worker by construction removes dictionary-file races;
the app window posts ("teach"/"dict_toggle"/"dict_delete") jobs and only
ever renders worker-posted snapshot copies). The tray "Open dictionary"
path is human-speed hand-editing, reconciled via reload_if_changed().
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Whisper feeds initial_prompt through its tokenizer and keeps at most 224
#: tokens (half its 448-token context). We stay comfortably under with a
#: chars/4 estimate and a 200-token budget — "hard-capped near" (plan §5),
#: erring low because overflow silently truncates the FRONT of the prompt.
_STT_PROMPT_TOKEN_BUDGET = 200


def _now_iso() -> str:
    """Local-time ISO-8601, same format History uses."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _est_tokens(text: str) -> int:
    """Conservative token estimate: ~4 chars/token, minimum 1."""
    return max(1, (len(text) + 3) // 4)


def _fresh_data() -> dict:
    return {"version": 1, "updated": None, "entries": []}


class Dictionary:
    """In-memory view over data/dictionary.json (plan §5)."""

    def __init__(
        self,
        path: Path,
        data: dict | None = None,
        autoenable: bool = True,
        max_prompt_entries: int = 60,
    ) -> None:
        self._path = Path(path)
        self._autoenable = autoenable
        self._max_prompt_entries = max_prompt_entries
        self._data = data if data is not None else _fresh_data()
        self._mtime_ns: int | None = self._stat_mtime_ns()
        self._reindex()

    # ------------------------------------------------------------------ #
    # Load / save / reload                                                #
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        path: Path,
        autoenable: bool = True,
        max_prompt_entries: int = 60,
    ) -> "Dictionary":
        """Load dictionary.json; NEVER crash on a bad file (plan §5).

        JSONDecodeError (or a non-dict top level) -> the corrupt file is
        renamed to dictionary.json.bad-<timestamp> and we start fresh.
        A missing file is simply a fresh dictionary. Malformed individual
        entries are logged and skipped, not fatal.
        """
        path = Path(path)
        data = cls._read_and_validate(path)
        return cls(path, data=data,
                   autoenable=autoenable, max_prompt_entries=max_prompt_entries)

    @staticmethod
    def _read_and_validate(path: Path) -> dict:
        if not path.exists():
            return _fresh_data()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("dictionary: could not read %s (%s) — starting fresh", path, e)
            return _fresh_data()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise json.JSONDecodeError("top level is not the expected object", raw, 0)
        except json.JSONDecodeError as e:
            quarantine = path.with_name(
                f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                os.replace(path, quarantine)
                log.warning(
                    "dictionary: %s is corrupt (%s) — quarantined to %s, starting fresh",
                    path.name, e, quarantine.name,
                )
            except OSError:
                log.exception("dictionary: corrupt AND could not quarantine %s", path)
            return _fresh_data()

        # Per-entry validation: skip garbage, keep the rest.
        good: list[dict] = []
        for i, entry in enumerate(data["entries"]):
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("canonical"), str)
                and entry["canonical"].strip()
                and isinstance(entry.get("misheard"), list)
            ):
                entry.setdefault("source", "manual")
                entry.setdefault("enabled", True)
                entry.setdefault("times_corrected", 0)
                entry.setdefault("times_applied", 0)
                entry.setdefault("first_seen", None)
                entry.setdefault("last_used", None)
                entry["misheard"] = [
                    str(m).strip().lower() for m in entry["misheard"] if str(m).strip()
                ]
                good.append(entry)
            else:
                log.warning("dictionary: skipping malformed entry %d: %r", i, entry)
        data["entries"] = good
        return data

    def save(self) -> None:
        """Atomic write: dictionary.json.tmp + os.replace (plan §5)."""
        self._data["updated"] = _now_iso()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        self._mtime_ns = self._stat_mtime_ns()
        log.info("dictionary: saved %d entries", len(self._data["entries"]))

    def reload_if_changed(self) -> bool:
        """Cheap stat check; reload from disk when the file changed under us.

        Called at the start of every dictate job (main._handle_dictate) so
        tray "Open dictionary" hand-edits take effect on the NEXT dictation.
        Returns True when a reload actually happened.
        """
        current = self._stat_mtime_ns()
        if current == self._mtime_ns:
            return False
        log.info("dictionary: %s changed on disk — reloading", self._path.name)
        self._data = self._read_and_validate(self._path)
        self._mtime_ns = self._stat_mtime_ns()
        self._reindex()
        return True

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    # Mutation                                                            #
    # ------------------------------------------------------------------ #

    def add_correction(self, misheard: str, canonical: str) -> dict:
        """Record one learned correction (plan §5).

        Existing entry whose canonical matches case-insensitively -> append
        the variant to its misheard list (deduped, lowercased) and bump
        times_corrected (+1 even when the variant already existed — the
        counter means "re-confirmed by the diff learner", §5 field table).
        Otherwise create a new entry with source="learned" and
        enabled=<learned_entry_autoenable>.

        Returns the affected entry dict.
        """
        canonical = canonical.strip()
        variant = misheard.strip().lower()
        entry = self._by_canonical.get(canonical.lower())
        if entry is not None:
            if variant and variant != entry["canonical"].lower() and variant not in entry["misheard"]:
                entry["misheard"].append(variant)
            entry["times_corrected"] = int(entry.get("times_corrected", 0)) + 1
            self._reindex()
            return entry
        entry = {
            "canonical": canonical,
            "misheard": [variant] if variant and variant != canonical.lower() else [],
            "source": "learned",
            "enabled": self._autoenable,
            "times_corrected": 1,
            "times_applied": 0,
            "first_seen": _now_iso(),
            "last_used": None,
        }
        self._data["entries"].append(entry)
        self._reindex()
        return entry

    def set_enabled(self, canonical: str, enabled: bool) -> bool:
        """Flip an entry's enabled flag (v2 U4 — the app window's per-entry
        toggle, executed on the worker via the ("dict_toggle", ...) job).

        Case-insensitive canonical lookup like add_correction. Returns True
        when an entry was found (caller saves); a missing canonical is a
        logged no-op returning False.
        """
        entry = self._by_canonical.get(canonical.strip().lower())
        if entry is None:
            log.warning("dictionary: set_enabled(%r) — no such entry", canonical)
            return False
        entry["enabled"] = bool(enabled)
        self._reindex()
        return True

    def remove(self, canonical: str) -> bool:
        """Drop an entry entirely (v2 U4 — the app window's Delete button,
        executed on the worker via the ("dict_delete", ...) job).

        Case-insensitive canonical lookup. Returns True when an entry was
        removed (caller saves); a missing canonical is a logged no-op
        returning False.
        """
        entry = self._by_canonical.get(canonical.strip().lower())
        if entry is None:
            log.warning("dictionary: remove(%r) — no such entry", canonical)
            return False
        self._data["entries"] = [e for e in self._data["entries"]
                                 if e is not entry]
        self._reindex()
        return True

    def note_applied(self, accepted_text: str) -> int:
        """Bump times_applied/last_used for every ENABLED canonical that
        appears (case-insensitive, word-bounded) in accepted output.

        Called from the learn step after each accepted dictation (plan §5:
        times_applied = "how often the canonical form appeared in accepted
        output" — the prompt-slot ranking signal). Returns how many entries
        were bumped; caller saves if > 0.
        """
        if not accepted_text:
            return 0
        bumped = 0
        now = _now_iso()
        for entry in self._data["entries"]:
            if not entry.get("enabled", True):
                continue  # disabled entries are dormant everywhere
            pattern = r"\b" + re.escape(entry["canonical"]) + r"\b"
            if re.search(pattern, accepted_text, flags=re.IGNORECASE):
                entry["times_applied"] = int(entry.get("times_applied", 0)) + 1
                entry["last_used"] = now
                bumped += 1
        return bumped

    # ------------------------------------------------------------------ #
    # Prompt builders — pure functions of the entry list (plan §5)        #
    # ------------------------------------------------------------------ #

    def _ranked_enabled(self) -> list[dict]:
        """Enabled entries ranked by times_applied desc, then last_used
        desc (ISO strings sort lexicographically; None ranks last), capped
        at max_dictionary_prompt_entries."""
        enabled = [e for e in self._data["entries"] if e.get("enabled", True)]
        enabled.sort(key=lambda e: e.get("last_used") or "", reverse=True)
        enabled.sort(key=lambda e: int(e.get("times_applied", 0)), reverse=True)
        return enabled[: self._max_prompt_entries]

    def stt_prompt(self) -> str:
        """'Glossary: <canonical>, <canonical>, ...' for Whisper's
        initial_prompt; "" when there is nothing to bias toward.

        Doubly capped: max_dictionary_prompt_entries AND a ~200-token
        estimate so we never brush Whisper's 224-token prompt window.
        """
        names: list[str] = []
        budget = _STT_PROMPT_TOKEN_BUDGET - _est_tokens("Glossary: ")
        for entry in self._ranked_enabled():
            cost = _est_tokens(entry["canonical"]) + 1  # +1 for ", "
            if cost > budget:
                # Greedy skip, not break: one oversized canonical must not
                # starve every lower-ranked (and probably shorter) name.
                continue
            names.append(entry["canonical"])
            budget -= cost
        if not names:
            return ""
        return "Glossary: " + ", ".join(names)

    def cleanup_context(self) -> str:
        """Newline block of '- "misheard" → "canonical"' pairs for the
        Claude system prompt (same ranking/cap); "" when empty.

        Entries with no recorded misheard variants (fresh manual adds) still
        contribute a line mapping the canonical to itself-in-lowercase, so
        Haiku knows the exact casing to enforce.
        """
        lines: list[str] = []
        for entry in self._ranked_enabled():
            variants = entry["misheard"] or [entry["canonical"].lower()]
            for variant in variants:
                lines.append(f'- "{variant}" → "{entry["canonical"]}"')
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Introspection                                                       #
    # ------------------------------------------------------------------ #

    @property
    def entries(self) -> list[dict]:
        return self._data["entries"]

    def _reindex(self) -> None:
        """Case-insensitive lookups (plan §5): misheard-variant -> entry
        and canonical -> entry. Disabled entries ARE indexed — matching by
        canonical must find them so counters accumulate in one place —
        but every prompt/apply path filters on enabled."""
        self._by_canonical: dict[str, dict] = {}
        self._by_misheard: dict[str, dict] = {}
        for entry in self._data["entries"]:
            self._by_canonical[entry["canonical"].lower()] = entry
            for variant in entry["misheard"]:
                self._by_misheard[variant.lower()] = entry

    def lookup_misheard(self, variant: str) -> dict | None:
        """Case-insensitive misheard-variant lookup (enabled entries only)."""
        entry = self._by_misheard.get(variant.strip().lower())
        if entry is not None and entry.get("enabled", True):
            return entry
        return None
