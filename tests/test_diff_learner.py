"""Unit tests for the pure diff learner — permissive rewrite (2026-07-10).

Philosophy under test (Nicolas's directive, supersedes the plan §6 gates):
a correction the user actually made is respected and learned — no
proper-noun gate, no stopword-only-span gate. The only refusals left are
the two that reject EDITS rather than CORRECTIONS: the global rewrite
guard and the per-pair similarity gate. Case-only fixes ('claude' ->
'Claude' mid-sentence) now learn too.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_diff_learner.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_diff_learner.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.diff_learner import Correction, extract_corrections  # noqa: E402


# ---------------------------------------------------------------------- #
# The original plan's required cases (1, 2, 4 unchanged; 3 flipped by the
# permissive rewrite)                                                      #
# ---------------------------------------------------------------------- #

def test_plan_case_1_learns_exactly_one_pair() -> None:
    """'talk to whisper flow' -> 'talk to Wispr Flow' learns EXACTLY the
    two-token pair — the case-only 'flow'->'Flow' neighbor is fused into
    the replace span, and the case-only pass must NOT learn it a second
    time (absorbed-index bookkeeping)."""
    out = extract_corrections("talk to whisper flow", "talk to Wispr Flow")
    assert out == [Correction(misheard="whisper flow", canonical="Wispr Flow")], out


def test_plan_case_2_full_rewrite_learns_nothing() -> None:
    """Global rewrite guard: user replaced the sentence, not a word."""
    cleaned = "let's meet tomorrow to discuss the quarterly budget review"
    edited = "the Porsche interview got moved to next Friday morning instead"
    assert extract_corrections(cleaned, edited) == []


def test_grammar_polish_learns_the_word_fix() -> None:
    """FLIPPED from the old plan case 3: 'its' -> \"it's\" is a correction
    the user made, so the permissive learner respects it (the old stopword
    gate refused it). 'i' -> 'I' stays unlearned — sentence-initial case."""
    out = extract_corrections("i think its good", "I think it's good.")
    assert out == [Correction(misheard="its", canonical="it's")], out


def test_plan_case_4_dissimilar_common_learns_nothing() -> None:
    """'meet at 3' -> 'meet at 4': ratio('3','4') = 0.0 < 0.45 — a content
    change, not a mishearing."""
    assert extract_corrections("meet at 3", "meet at 4") == []


# ---------------------------------------------------------------------- #
# The two remaining guards (rewrite + similarity)                          #
# ---------------------------------------------------------------------- #

def test_content_edit_rejected_by_similarity() -> None:
    """'the meeting' -> 'our standup' (~0.18) is a content edit, not a
    mishearing — the similarity gate is the FP filter that stays."""
    out = extract_corrections(
        "we moved the meeting to friday", "we moved our standup to friday"
    )
    assert out == [], out


def test_min_similarity_is_a_working_knob() -> None:
    """config's min_correction_similarity actually gates: raising it above
    the whisper-flow ratio (0.909) turns case 1 off."""
    out = extract_corrections(
        "talk to whisper flow", "talk to Wispr Flow", min_similarity=0.95
    )
    assert out == [], out


def test_many_distinct_corrections_still_reads_as_rewrite() -> None:
    """The repeat-aware guard must NOT open the door to genuine rewrites: an
    edit that replaces many DIFFERENT words is one large replace op, so its
    effective changed-token count exceeds max(8, 20%) and the token threshold
    rejects it (deduping only collapses IDENTICAL repeats, not distinct ones)."""
    cleaned = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    edited = "Apple Banana Cherry Date Elder Fig Grape Honey Ivy Jack"
    assert extract_corrections(cleaned, edited) == []


def test_identical_and_empty_inputs() -> None:
    assert extract_corrections("same text", "same text") == []
    assert extract_corrections("", "anything") == []
    assert extract_corrections("anything", "") == []


# ---------------------------------------------------------------------- #
# Permissive behavior — corrections the OLD gates refused now learn        #
# ---------------------------------------------------------------------- #

def test_lone_sentence_initial_name_fix_is_learned() -> None:
    """THE live complaint (2026-07-10): a single sentence-initial
    'cloud' -> 'Claude' fix must learn. The old proper-noun gate refused it
    (capital proves nothing at sentence start, zipf 3.76 not rare enough);
    the gate is gone — a respelled word is a correction wherever it sits."""
    out = extract_corrections("cloud often gets this wrong",
                              "Claude often gets this wrong")
    assert out == [Correction(misheard="cloud", canonical="Claude")], out


def test_common_word_accent_fix_is_learned() -> None:
    """FLIPPED: 'sleep' -> 'sleepy' is exactly the accent-mishearing class
    the dictionary is FOR now — not just proper nouns. The old zipf-band
    reasoning ('would learn junk') is retired; junk control is the
    Dictionary tab + periodic audit."""
    out = extract_corrections("i feel so sleep today", "i feel so sleepy today")
    assert out == [Correction(misheard="sleep", canonical="sleepy")], out


def test_stopword_only_old_span_is_learned() -> None:
    """FLIPPED: 'as your' -> 'Azure' is a classic real-world mishearing the
    old stopword-only filter refused by design. It learns now — the pairs
    are applied contextually by Haiku, never by blind regex, so a common
    phrase as the misheard side degrades gracefully."""
    out = extract_corrections(
        "deploy it on as your today", "deploy it on Azure today"
    )
    assert out == [Correction(misheard="as your", canonical="Azure")], out


def test_sentence_initial_respelling_is_learned() -> None:
    """FLIPPED: 'there' -> 'Their' at sentence start was refused by the old
    capital-proves-nothing rule; it's a respelling the user made, so it
    learns (canonical keeps the user's exact casing)."""
    out = extract_corrections("there report was late", "Their report was late")
    assert out == [Correction(misheard="there", canonical="Their")], out


def test_rare_word_fix_still_learned() -> None:
    """'kalify' -> 'Coolify' learned under the old rules AND the new ones
    (similarity ~0.62 clears the gate) — no regression for jargon."""
    out = extract_corrections("kalify is down", "Coolify is down")
    assert out == [Correction(misheard="kalify", canonical="Coolify")], out


# ---------------------------------------------------------------------- #
# Case-only pass — NEW: pure capitalization fixes learn too                #
# ---------------------------------------------------------------------- #

def test_case_only_fix_mid_sentence_is_learned() -> None:
    """'claude' -> 'Claude' with no letters changed is invisible to the
    replace-opcode loop (lowercased tokens compare equal) — the case pass
    catches it. Mid-sentence, so the capital is deliberate."""
    out = extract_corrections("ask claude about it", "ask Claude about it")
    assert out == [Correction(misheard="claude", canonical="Claude")], out


def test_case_only_fix_at_sentence_start_not_learned() -> None:
    """A case-ONLY change at a sentence start is indistinguishable from
    ordinary English capitalization — skipped. (A respelling at sentence
    start still learns; see test_sentence_initial_respelling_is_learned.)"""
    assert extract_corrections("claude gets it wrong",
                               "Claude gets it wrong") == []
    assert extract_corrections("done. claude agreed",
                               "done. Claude agreed") == []


def test_case_only_stopword_not_learned() -> None:
    """'i' -> 'I' is grammar the cleanup layer already handles — the ONE
    place stopwords still gate anything."""
    assert extract_corrections("well i said so", "well I said so") == []


# ---------------------------------------------------------------------- #
# Span mechanics (unchanged by the rewrite)                                #
# ---------------------------------------------------------------------- #

def test_three_token_span_with_case_growth() -> None:
    """2-token replace grows across a case-changed neighbor into the full
    3-token proper noun (MAX_SPAN_TOKENS boundary)."""
    out = extract_corrections(
        "send it to data kai house tonight", "send it to Theta Chi House tonight"
    )
    assert out == [Correction(misheard="data kai house", canonical="Theta Chi House")], out


def test_attached_punctuation_stripped() -> None:
    """'flow,' learns as 'flow' — punctuation rides along for alignment but
    never enters the stored pair."""
    out = extract_corrections(
        "i love whisper flow, honestly", "i love Wispr Flow, honestly"
    )
    assert out == [Correction(misheard="whisper flow", canonical="Wispr Flow")], out


def test_two_independent_corrections_in_one_dictation() -> None:
    """Two separate fixes both learn (and nothing else does)."""
    cleaned = "ping whisper flow about the data kai event"
    edited = "ping Wispr Flow about the Theta Chi event"
    out = extract_corrections(cleaned, edited)
    assert sorted((c.misheard, c.canonical) for c in out) == [
        ("data kai", "Theta Chi"),
        ("whisper flow", "Wispr Flow"),
    ], out


def test_span_growth_never_crosses_sentence_boundary_right() -> None:
    """Review-caught regression (Chunk 6 rejection): a name fix at the END
    of a sentence must not fuse the NEXT sentence's ordinary sentence-initial
    capital into the learned pair — and the case pass must not learn that
    sentence-initial 'hello' -> 'Hello' on its own either."""
    out = extract_corrections(
        "ping whisper flow. hello world", "ping Wispr Flow. Hello world"
    )
    assert out == [Correction(misheard="whisper flow", canonical="Wispr Flow")], out


def test_span_growth_never_crosses_sentence_boundary_left() -> None:
    """Mirror of the right-boundary case: a case-only-changed token in the
    PREVIOUS sentence must not be absorbed leftward into the pair — but
    since it's a deliberate mid-sentence capitalization, the case pass now
    learns it as its OWN pair (permissive rewrite)."""
    out = extract_corrections(
        "tell the world. whisper flow rocks", "tell the World. Wispr Flow rocks"
    )
    assert sorted((c.misheard, c.canonical) for c in out) == [
        ("whisper flow", "Wispr Flow"),
        ("world", "World"),
    ], out


def test_same_correction_repeated_is_not_a_rewrite() -> None:
    """Live-reported regression: fixing the SAME mishearing in many places is
    the strongest teaching signal, yet the old rewrite guard counted total
    changed tokens and rejected it. Repeated identical corrections must
    collapse to one learned pair, not trip the guard."""
    cleaned = ("i asked cloud about it then cloud said no so i told cloud "
               "again and cloud repeated the same thing and cloud was wrong")
    edited = ("i asked Claude about it then Claude said no so i told Claude "
              "again and Claude repeated the same thing and Claude was wrong")
    out = extract_corrections(cleaned, edited)
    assert out == [Correction(misheard="cloud", canonical="Claude")], out


# ---------------------------------------------------------------------- #
# Plain-python runner (same convention as test_app_state.py)              #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
