"""Diff learner for flow-clone (Chunk 6, plan §6; permissive rewrite
2026-07-10) — the pure module.

Input: the text the pipeline PRODUCED (pasted/shown), and the text the user
made it INTO (in-place fix via the UIA watcher, or Save & teach). Output:
the corrections the user made, as (misheard -> canonical) pairs for
dictionary.add_correction().

Philosophy (2026-07-10, Nicolas's directive — supersedes the original plan
§6 gating): if the user corrected something, that IS the signal — respect
it and learn it. The dictionary is not just for proper nouns; it's for any
word Murmur keeps getting wrong for THIS user (accent mishearings, names,
jargon, "claud" -> "Claude"). Junk control moved from the learner to the
user: the Dictionary tab (enable/disable/delete) plus a periodic audit.
The old proper-noun gate (capitalization/zipf-rarity) and the stopword-only
filter are deliberately GONE — they made the learner refuse real,
repeatedly-made corrections ("cloud" -> "claude" uncapitalized, "as your"
-> "Azure"). Note the pairs are applied CONTEXTUALLY: only Whisper's
glossary prompt and Haiku's system prompt see them — nothing does blind
regex replacement — so an over-broad pair degrades gracefully.

Algorithm:
1. Tokenize both strings on whitespace. Punctuation stays attached to its
   token for alignment, but is STRIPPED when comparing and when storing —
   so 'flow,' learns as 'flow'. Comparison is case-insensitive; the
   canonical is stored case-sensitively from the user's edit.
2. difflib.SequenceMatcher over the lowercased token lists; consider only
   'replace' opcodes where both sides are 1-3 tokens. A replace span is
   grown into ADJACENT case-only-changed tokens, still capped at 3 per
   side and never across a sentence boundary ('talk to whisper flow' ->
   'talk to Wispr Flow' must learn the two-token pair).
3. Global rewrite guard (repeat-aware): count insert/delete tokens in full
   but each DISTINCT replace pair only ONCE, then if that effective count >
   max(8, 20% of the text) the user rewrote the passage, not corrected a
   word -> learn NOTHING. This is the guard that keeps the watcher from
   "learning" a paragraph the user simply kept writing/revising.
4. The ONE remaining per-candidate filter: SequenceMatcher(None, old,
   new).ratio() >= min_similarity — the correction must sound/spell like
   the mistake ('claud' -> 'Claude' passes; 'the meeting' -> 'our standup'
   ~0.18 is a content edit, not a mishearing, rejected).
5. Case-only pass: tokens the matcher saw as EQUAL (same lowercase) but
   whose exact case the user changed are corrections too — 'claude' ->
   'Claude' mid-sentence learns. Sentence-initial case changes are skipped
   (ordinary English capitalization proves nothing there), as are
   stopwords ('i' -> 'I' is grammar the cleanup layer already handles).
6. Caller feeds each Correction to the dictionary, saves, and toasts
   'Learned: "x" → "Y"' — learning is always visible.
"""

from __future__ import annotations

import logging
import string
from dataclasses import dataclass
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

#: Replace spans may be at most this many tokens per side.
MAX_SPAN_TOKENS = 3

#: Case-only changes to these are grammar noise ('i' -> 'I'), not personal
#: vocabulary — the ONLY place stopwords still gate anything (the old
#: stopword-only-old-span filter on replace pairs is gone; see module
#: docstring).
STOPWORDS = frozenset(
    """a an and are as at be but by for from had has have he her his i if in
    is it its me my not of on or our she so that the their them they this to
    was we were will with you your""".split()
)

#: Stripped from token edges when comparing/storing (plan: "punctuation
#: attached, strip when comparing" — includes curly quotes/dashes Claude
#: cleanup likes to emit).
_PUNCT = string.punctuation + "‘’“”–—…"

#: A token ending in one of these terminates a sentence; the next token is
#: sentence-initial, so its capital alone proves nothing.
_SENTENCE_END = (".", "!", "?", ":", ";")


@dataclass(frozen=True)
class Correction:
    misheard: str      # lowercased span from the pipeline's text
    canonical: str     # exact-case span from the user's edit


def _core(token: str) -> str:
    """Token with attached edge punctuation stripped ('flow,' -> 'flow')."""
    return token.strip(_PUNCT)


def extract_corrections(
    cleaned: str,
    edited: str,
    min_similarity: float = 0.45,
) -> list[Correction]:
    """Diff pipeline-produced text vs user-corrected text; return learned
    pairs.

    Pure function — no I/O, no dictionary access. Permissive by design
    (module docstring): the only refusals left are the global-rewrite guard
    and the per-pair similarity gate, both of which reject EDITS (new
    content) rather than CORRECTIONS (same content, fixed words).
    """
    if not cleaned or not edited or cleaned == edited:
        return []

    old_tokens = cleaned.split()
    new_tokens = edited.split()
    if not old_tokens or not new_tokens:
        return []

    old_core = [_core(t) for t in old_tokens]
    new_core = [_core(t) for t in new_tokens]
    old_lower = [t.lower() for t in old_core]
    new_lower = [t.lower() for t in new_core]

    matcher = SequenceMatcher(a=old_lower, b=new_lower, autojunk=False)
    opcodes = matcher.get_opcodes()

    # --- 3. Global rewrite guard (repeat-aware) ----------------------- #
    # The guard rejects genuine sentence rewrites, NOT the correction of one
    # recurring mishearing. So count churn that signals rewriting: every
    # insert/delete token fully, but each DISTINCT replace pair only once.
    # Fixing "cloud"->"Claude" in five places is one correction applied five
    # times (the strongest teaching signal) — it must not read as a rewrite.
    # (A real rewrite still trips this: it produces either large insert/delete
    # churn or a big multi-token replace op, both of which count in full.)
    text_tokens = max(len(old_tokens), len(new_tokens))
    insert_delete_tokens = 0
    distinct_replace_pairs: set[tuple] = set()
    distinct_replace_tokens = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if tag == "replace":
            key = (tuple(old_lower[i1:i2]), tuple(new_lower[j1:j2]))
            if key not in distinct_replace_pairs:
                distinct_replace_pairs.add(key)
                distinct_replace_tokens += (i2 - i1) + (j2 - j1)
        else:  # insert / delete — real restructuring, counted in full
            insert_delete_tokens += (i2 - i1) + (j2 - j1)

    effective_changed = distinct_replace_tokens + insert_delete_tokens
    if effective_changed > max(8, 0.20 * text_tokens):
        log.info(
            "diff learner: global rewrite (%d effective changed tokens of %d)"
            " — learning nothing", effective_changed, text_tokens,
        )
        return []

    # --- 2 + 4. Candidate spans + the similarity gate ------------------ #
    corrections: dict[tuple[str, str], Correction] = {}
    #: new-token indices consumed by replace spans (incl. case-only growth) —
    #: the case-only pass below must not learn those tokens a second time.
    absorbed_j: set[int] = set()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "replace":
            continue
        if not (1 <= i2 - i1 <= MAX_SPAN_TOKENS and 1 <= j2 - j1 <= MAX_SPAN_TOKENS):
            continue

        # Grow into adjacent case-only-changed tokens (equal lowered, exact
        # case differs) — they're part of the same proper noun. Capped so
        # neither side exceeds MAX_SPAN_TOKENS, and NEVER across a sentence
        # boundary: a case-only change on the far side of a '.'/'!'/'?' is
        # ordinary sentence-initial capitalization, not part of the noun
        # (review-caught: without this, "whisper flow. hello" ->
        # "Wispr Flow. Hello" fused 'hello' into the learned pair).
        while (
            i1 > 0 and j1 > 0
            and i2 - i1 < MAX_SPAN_TOKENS and j2 - j1 < MAX_SPAN_TOKENS
            and old_lower[i1 - 1] == new_lower[j1 - 1]
            and old_core[i1 - 1] != new_core[j1 - 1]
            # The token being absorbed must not END a sentence — if it does,
            # the current span starts a new sentence and the token belongs
            # to the previous one. Check both texts; either boundary stops.
            and not new_tokens[j1 - 1].endswith(_SENTENCE_END)
            and not old_tokens[i1 - 1].endswith(_SENTENCE_END)
        ):
            i1 -= 1
            j1 -= 1
        while (
            i2 < len(old_core) and j2 < len(new_core)
            and i2 - i1 < MAX_SPAN_TOKENS and j2 - j1 < MAX_SPAN_TOKENS
            and old_lower[i2] == new_lower[j2]
            and old_core[i2] != new_core[j2]
            # The last token already IN the span must not end a sentence —
            # if it does, the candidate token is sentence-initial and its
            # capital is ordinary. Check both texts; either boundary stops.
            and not new_tokens[j2 - 1].endswith(_SENTENCE_END)
            and not old_tokens[i2 - 1].endswith(_SENTENCE_END)
        ):
            i2 += 1
            j2 += 1

        absorbed_j.update(range(j1, j2))

        old_span_core = [c for c in old_core[i1:i2] if c]
        new_span_core = [c for c in new_core[j1:j2] if c]
        if not old_span_core or not new_span_core:
            continue  # pure-punctuation tokens stripped to nothing

        misheard = " ".join(c.lower() for c in old_span_core)
        canonical = " ".join(new_span_core)

        # Case-only replace spans belong to the case pass below, which
        # applies its own (sentence-initial / stopword) sanity checks.
        if misheard == canonical.lower():
            continue

        # Similarity: the correction must sound/spell like the mistake.
        ratio = SequenceMatcher(None, misheard, canonical.lower()).ratio()
        if ratio < min_similarity:
            log.debug(
                "diff learner: rejected %r -> %r (ratio %.2f < %.2f)",
                misheard, canonical, ratio, min_similarity,
            )
            continue

        corrections[(misheard, canonical)] = Correction(misheard, canonical)

    # --- 5. Case-only pass --------------------------------------------- #
    # The matcher never emits a replace opcode for a pure case change (it
    # compares lowercased tokens), so 'claude' -> 'Claude' is invisible to
    # the loop above. Scan the equal opcodes for tokens whose exact case the
    # user changed and learn each one — skipping sentence-initial positions
    # (ordinary capitalization), stopwords ('i' -> 'I' grammar noise), and
    # tokens a grown replace span already consumed.
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            i, j = i1 + k, j1 + k
            if j in absorbed_j:
                continue
            if old_core[i] == new_core[j] or not old_core[i]:
                continue
            if old_lower[i] in STOPWORDS:
                continue
            if j == 0 or new_tokens[j - 1].endswith(_SENTENCE_END):
                continue  # sentence-initial capital proves nothing
            misheard = old_lower[i]
            canonical = new_core[j]
            if misheard == canonical:
                continue  # de-capitalized to the exact stored form — no-op
            corrections[(misheard, canonical)] = Correction(misheard, canonical)

    return list(corrections.values())
