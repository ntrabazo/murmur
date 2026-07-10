"""UIA correction watcher for flow-clone (v2 U3, plan F3) — the Wispr-style
"learn from what you fix" loop, rebuilt for the auto-paste world.

With the review popup gone (U2) there is no edit box to diff anymore. This
module watches the TARGET app instead: after a successful paste, a daemon
thread polls the focused control's text via UI Automation for up to
~45 seconds. If the user fixes a word in place ("whisper flow" ->
"Wispr Flow") and the text then sits still for two polls, the watcher diffs
the pasted text against what's on screen (diff_learner.extract_corrections)
and posts the result BACK to the pipeline worker as a
("learned_corrections", [Correction, ...]) job. The worker — and only the
worker — mutates the Dictionary (single-writer invariant, plan §3.5); this
module never touches AppState, Dictionary, ui_q, or tkinter.

Fail-silent contract (plan U3): a watcher crash, an unreadable control, a
COM hiccup — none of it may ever surface a toast, block the worker, or
leave COM initialized. run() CoInitializes first, CoUninitializes in a
finally, and wraps everything so no exception escapes the thread.

Stop conditions (each ends the watch FOR GOOD — no resume, no retry):
  - cancel Event set (a newer dictation superseded this watcher);
  - duration expired;
  - foreground window != the paste target (the user left — following them
    around or resuming when they come back would be spooky);
  - the focused control exposes neither TextPattern nor ValuePattern, or
    any UIA read fails (this app can't be observed);
  - one ("learned_corrections", ...) job was posted (one-shot).

Testability: the UIA/win32 specifics live in two thin adapter functions
(_default_read_focused_text / _default_get_foreground) injected via the
constructor, and find_pasted_region is a pure module-level function — the
whole poll loop runs headlessly in the suite with fakes, creating zero UIA
objects (the uiautomation import below and the thread's CoInitialize/
CoUninitialize pair still run; both are inert without UIA calls).

find_pasted_region semantics (plan U3, one deliberate refinement): an exact
substring hit returns the pasted text's slice verbatim. Otherwise the
window is SEEDED from SequenceMatcher.find_longest_match (aligned so the
pasted text's start maps into the control text), sized at 1.25x the pasted
length, clamped to bounds and expanded to whitespace boundaries — then
GATED on SequenceMatcher.ratio(window, pasted) >= 0.60. The plan's literal
gate (single longest match >= 60% of the pasted length) rejects its own
required case — a one-word fix mid-text splits the match into two blocks
each under 60% ("talk to whisper flow" -> "talk to Wispr Flow" has a
longest block of 10/20 chars) — so the 60% threshold is applied to the
window's overall similarity instead, which passes real corrections (~0.9)
and still fails safe (None) on scrolled-away or mangled text.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from difflib import SequenceMatcher

from src.diff_learner import extract_corrections

log = logging.getLogger(__name__)

# Imported EAGERLY and guarded, for one load-bearing side effect:
# uiautomation calls SetProcessDpiAwareness(PerMonitorDpiAware) at import
# (uiautomation.py module level). main.py imports this module at its top,
# BEFORE tk.Tk() exists — so the process's DPI regime is fixed once at
# startup instead of flipping mid-session on the first watcher poll, which
# would re-scale the already-positioned pill/toasts (the same hazard, and
# the same import-before-Tk recipe, as U4's customtkinter — plan risk 1).
# On import failure the watcher degrades to "controls can't be observed"
# (reader returns None) rather than ever blocking app startup.
try:
    import uiautomation as _uia
except Exception:  # pragma: no cover — environment-specific
    _uia = None
    log.warning("uiautomation unavailable — correction watching disabled",
                exc_info=True)

#: A fuzzy window must be at least this similar to the pasted text to count
#: as "our text, edited" — below it the text scrolled away / was rewritten
#: beyond recognition and the watcher must not diff garbage.
_FUZZY_MIN_RATIO = 0.60

#: Fuzzy window size as a multiple of the pasted length — headroom for the
#: user's fix being longer than what it replaced.
_WINDOW_FACTOR = 1.25


# ---------------------------------------------------------------------- #
# Pure region finder                                                      #
# ---------------------------------------------------------------------- #

def find_pasted_region(control_text: str, pasted: str) -> str | None:
    """Locate (a superset window around) the pasted text inside the focused
    control's full text. Pure — no UIA, no I/O.

    Returns the region to diff against the pasted text, or None when our
    text is not findable (scrolled away / too mangled / mid-edit churn) —
    the caller treats None as "nothing to evaluate this poll".
    """
    if not control_text or not pasted:
        return None

    # Exact containment: the text is still there (possibly with edits
    # elsewhere in the document that are none of our business).
    if pasted in control_text:
        return pasted

    sm = SequenceMatcher(None, control_text.lower(), pasted.lower(),
                         autojunk=False)
    m = sm.find_longest_match(0, len(control_text), 0, len(pasted))
    if m.size == 0:
        return None

    # Seed the window so the pasted text's start position maps into the
    # control text, then take 1.25x the pasted length, clamped to bounds.
    start = max(0, m.a - m.b)
    end = min(len(control_text), start + round(len(pasted) * _WINDOW_FACTOR))

    # Expand (never cut) to whitespace boundaries — a window edge landing
    # mid-word would hand the diff learner half a token.
    while start > 0 and not control_text[start - 1].isspace():
        start -= 1
    while end < len(control_text) and not control_text[end].isspace():
        end += 1

    region = control_text[start:end].strip()
    if not region:
        return None

    # The 60% gate (see module docstring): applied to the window's overall
    # similarity to the pasted text, so a one-word mid-text fix passes and
    # unrelated text fails safe.
    ratio = SequenceMatcher(None, region.lower(), pasted.lower()).ratio()
    if ratio < _FUZZY_MIN_RATIO:
        log.debug("find_pasted_region: best window ratio %.2f < %.2f — "
                  "not our text", ratio, _FUZZY_MIN_RATIO)
        return None
    return region


# ---------------------------------------------------------------------- #
# Default adapters — the ONLY UIA/win32 code in this module               #
# ---------------------------------------------------------------------- #

def _default_get_foreground() -> int:
    """Current foreground hwnd (worker-independent — this runs on the
    watcher thread; GetForegroundWindow is thread-safe)."""
    import win32gui
    return win32gui.GetForegroundWindow()


def _default_read_focused_text() -> str | None:
    """Full text of the currently focused control via UI Automation:
    TextPattern first (rich editors), ValuePattern fallback (plain edits),
    None when the control supports neither — the caller stops watching.
    (GetPattern swallows COMError and returns None for unsupported
    patterns — verified against uiautomation 2.0.29.)

    Any COMError/AttributeError from a hostile control propagates to run()'s
    catch-all and silently ends the watch — deliberate: retry-spamming UIA
    against a control that just refused is how watchers become noticeable.
    """
    if _uia is None:
        return None
    control = _uia.GetFocusedControl()
    if control is None:
        return None
    pattern = control.GetPattern(_uia.PatternId.TextPattern)
    if pattern is not None:
        return pattern.DocumentRange.GetText(-1)
    pattern = control.GetPattern(_uia.PatternId.ValuePattern)
    if pattern is not None:
        return pattern.Value
    return None


# ---------------------------------------------------------------------- #
# The watcher thread                                                      #
# ---------------------------------------------------------------------- #

class CorrectionWatcher(threading.Thread):
    """One paste -> one watcher (daemon, fire-and-forget, never joined).

    Spawned by main._do_inject only on a successful paste; superseded (its
    `cancel` Event set) when a newer dictation pastes. Its ONLY output is a
    single ("learned_corrections", [Correction, ...]) put on job_q.
    """

    def __init__(
        self,
        target_hwnd: int,
        pasted_text: str,
        job_q: queue.Queue,
        cancel: threading.Event,
        poll_sec: float = 2.5,
        duration_sec: float = 45.0,
        min_similarity: float = 0.45,
        read_focused_text=None,   # injectable for headless tests
        get_foreground=None,      # injectable for headless tests
    ) -> None:
        super().__init__(name="correction-watcher", daemon=True)
        self.target_hwnd = target_hwnd
        self.pasted_text = pasted_text
        self.job_q = job_q
        self.cancel = cancel
        self.poll_sec = poll_sec
        self.duration_sec = duration_sec
        self.min_similarity = min_similarity
        self._read = (read_focused_text if read_focused_text is not None
                      else _default_read_focused_text)
        self._foreground = (get_foreground if get_foreground is not None
                            else _default_get_foreground)

    def run(self) -> None:
        # COM discipline (pywinauto UIA-threading recipe): every thread
        # that talks to UIA initializes COM itself and uninitializes it on
        # the way out, whatever happened in between.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            log.debug("correction watcher: CoInitialize failed — not watching",
                      exc_info=True)
            return
        try:
            self._watch()
        except Exception:
            # Fail-silent contract: log at DEBUG and die. A watcher bug must
            # never toast, touch AppState, or block anything.
            log.debug("correction watcher died", exc_info=True)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _watch(self) -> None:
        deadline = time.monotonic() + self.duration_sec
        prev_region: str | None = None
        while not self.cancel.wait(self.poll_sec):
            if time.monotonic() >= deadline:
                log.debug("correction watcher: duration expired — done")
                return

            # (a)+(b) supersession is the wait() above; foreground check:
            # the user left the target window -> stop for good (no resume —
            # one departure ends the watch, plan U3 "unspooky" rule).
            if self._foreground() != self.target_hwnd:
                log.debug("correction watcher: foreground left target — done")
                return

            # (c) read the focused control; unobservable -> stop for good.
            text = self._read()
            if text is None:
                log.debug("correction watcher: control not observable — done")
                return

            region = find_pasted_region(text, self.pasted_text)
            if region is None:
                # Mid-edit churn / text momentarily unfindable: keep polling,
                # but a vanished region also resets the stability gate.
                prev_region = None
                continue

            # Stability gate: only diff when the region sat still for two
            # consecutive polls (the user stopped typing) AND it actually
            # differs from what we pasted.
            if region == prev_region and region != self.pasted_text:
                corrections = extract_corrections(
                    self.pasted_text, region, self.min_similarity
                )
                if corrections:
                    log.info("correction watcher: %d correction(s) observed",
                             len(corrections))
                    self.job_q.put(("learned_corrections", corrections))
                    return  # one shot per watcher
                # Stable diff but nothing learnable (rewrite/case-only):
                # keep watching — the user may still fix a proper noun.
            prev_region = region
        log.debug("correction watcher: superseded — done")
