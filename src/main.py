"""flow-clone entrypoint (Chunks 4-7, v2 auto-paste): state machine +
queues + tray, wired end-to-end, with clipboard-safe AUTO-injection and the
Chunk 7 hardening layer (preflight startup checks, rotating log, real tray
Pause/Resume).

v2 (U2): the review popup is gone. A dictation flows straight through
RECORDING -> TRANSCRIBING -> CLEANING -> INJECTING -> IDLE on the single
worker thread — release the hotkey and the cleaned text lands in the app
you were dictating into, no Enter, no window. The one guard replacing the
popup: a foreground precheck (injector.foreground_verdict) — if the user
switched to a DIFFERENT app while the pipeline was busy, we never paste and
never yank focus back; the text goes to History with outcome
"window_changed" and a toast says so.

Threading model (plan §3.5, v2 flow):

    main thread                     worker thread              pynput hook thread     PortAudio thread
    -----------                     -------------              ------------------     ----------------
    tk root (withdrawn)             loop:                      on_press(hotkey):      callback:
    mainloop()                        job = job_q.get()          IDLE->RECORDING        if armed:
    poll ui_q every 50ms              stt.transcribe()           recorder.arm()           append chunk
      ("toast",..)   -> tray toast    cleaner.clean()          on_release(hotkey):
      ("status",..)  -> tray dot      foreground_verdict()       disarm_and_get() FIRST
        + status pill                   "other" -> History       debounce the RESULT
      ("open_app",)  -> app window      else paste_into()      job_q.put(("dictate",..))
      ("dict_changed", snapshot)      history + learn step
        -> window dict tab            spawn CorrectionWatcher
      ("app_refresh",)                  (daemon; on a user fix it
        -> window transcripts           posts ("learned_corrections",
                                        [...]) back onto job_q —
                                        dictionary mutation stays
                                        HERE, on this one worker)
                                      "teach"/"dict_toggle"/
                                        "dict_delete" jobs from the
                                        app window mutate the
                                        dictionary HERE too, then
                                        post a deepcopy snapshot

v2 (U4): the app window (src/app_window.py, customtkinter) is the face of
the tool — tray "Open Murmur" (also the icon's default action) opens a
dark window with the transcript list (copy / edit-to-teach), the dictionary
(per-entry enable toggle + delete), and a status footer. It replaces the
old HistoryViewer. Single-writer holds: the window posts jobs and renders
worker-posted snapshots; it never touches the Dictionary object.

v2 (U3): after each successful paste the worker fire-and-forgets a
CorrectionWatcher daemon thread (src/correction_watcher.py) that polls the
target control via UI Automation for ~45s; if the user fixes a word in
place, the watcher posts the diffed corrections back as a job and the
worker learns them (add_correction + "Learned:" toast + save). One live
watcher at a time: each new paste sets the previous watcher's cancel Event
before spawning the next.

    Hotkey is config["hotkey"] (Right Ctrl by default, changed from the
    original F9 per live-testing feedback — Fn-layer F-keys on gaming
    keyboards were inconvenient). The floating StatusIndicator pill
    (bottom-center, red=recording/amber=processing) mirrors the tray dot for
    visibility, since Windows 11 hides new tray icons by default.

Hook-thread discipline (plan §3.5, load-bearing): the pynput callbacks below
do ONLY state CAS + recorder.arm()/disarm_and_get() (pure in-memory) + a
queue.put + an async beep. recorder.open_stream() is called ONCE at startup,
never on the hotkey path.

Chunk-1-review finding, carried forward verbatim (plan §6 Chunk 4 header):
on_release calls recorder.disarm_and_get() UNCONDITIONALLY FIRST, and only
THEN applies the <150ms debounce check to the result — the debounce discards
the audio, it never skips the disarm call itself.

Failure modes (plan §6 Chunk 4, v2 amendments):
- Overlapping dictations -> impossible: a press in any non-IDLE state
  fails the IDLE->RECORDING CAS and just beeps.
- User switches apps during the 3-6s pipeline -> foreground_verdict says
  "other": no paste, no focus yank, History "window_changed" + toast.
- Worker exception anywhere -> caught at loop top, logged with traceback,
  state force-reset to IDLE, toast "error — see log".
- Esc pressed (2026-07-10) -> cancel whatever is in flight: while RECORDING
  the hook callback discards the audio directly (incl. a dual-mode latch);
  while TRANSCRIBING/CLEANING/INJECTING it sets cancel_event and the worker
  drops the result at the next stage boundary (text goes to History as
  "discarded" once it exists). The Esc keystroke is suppressed system-wide
  ONLY when it actually cancelled something; an idle Esc passes through.
- Quit from tray -> stops listener, joins worker via sentinel job (None),
  closes the audio stream, destroys root.
"""

from __future__ import annotations

import copy
import logging
import queue
import sys
import threading
import time
import tkinter as tk
import winsound
from pathlib import Path
from tkinter import messagebox

import win32gui

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app_state import AppState, State  # noqa: E402
# v2 U4 DPI ordering rule (plan risk #1): importing app_window imports
# customtkinter, which sets PROCESS-WIDE DPI awareness at import time.
# This import must stay at module top, BEFORE any tk.Tk() is created
# (main() and _fatal_dialog both create one at runtime), so every window
# in the process lives under a single DPI regime.
from src.app_window import ICON_PATH as APP_ICON_PATH  # noqa: E402
from src.app_window import AppWindow  # noqa: E402
from src.cleanup import Cleaner  # noqa: E402
from src.config import get_anthropic_key, load_config  # noqa: E402
from src.correction_watcher import CorrectionWatcher  # noqa: E402
from src.dictionary import Dictionary  # noqa: E402
from src.diff_learner import extract_corrections  # noqa: E402
from src.history import History  # noqa: E402
from src.hotkey import DEBOUNCE_SEC, DoubleTapTracker, HotkeyListener  # noqa: E402
from src.injector import foreground_verdict, paste_into  # noqa: E402
from src.logging_setup import configure_logging  # noqa: E402
from src.recorder import Recorder  # noqa: E402
from src.single_instance import (  # noqa: E402
    signal_show_window,
    start_show_window_listener,
)
from src.startup_check import run_startup_checks  # noqa: E402
from src.status_indicator import StatusIndicator  # noqa: E402
from src.stt import SttEngine  # noqa: E402
from src.tray import Tray  # noqa: E402

log = logging.getLogger("flowclone")

DICTIONARY_PATH = PROJECT_ROOT / "data" / "dictionary.json"
HISTORY_PATH = PROJECT_ROOT / "data" / "history.jsonl"
LOG_PATH = PROJECT_ROOT / "logs" / "flowclone.log"


def _set_windows_app_identity() -> None:
    """Give the process its own taskbar identity (AppUserModelID).

    Without this, Windows groups every window under pythonw.exe's identity
    and the taskbar shows the generic Python icon no matter what
    iconbitmap() says. With it, the taskbar uses OUR window icon (the
    gradient-soundwave .ico). Must run before any window exists — main()
    calls it first, so even the fatal-startup dialog is branded."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Murmur.FlowClone")
    except Exception:
        log.debug("could not set AppUserModelID", exc_info=True)


def _fatal_dialog(message: str) -> None:
    """Blocking error dialog for fatal startup failures (plan §6 Chunk 7).

    The ONE place a modal is allowed to interrupt — nothing else is running
    yet, and under pythonw (flow-clone.cmd) a stderr print is invisible, so
    this is what makes a silent double-launch or a missing key debuggable
    instead of a mysteriously-dead tray icon."""
    try:
        dlg_root = tk.Tk()
        dlg_root.withdraw()
        messagebox.showerror("Murmur — cannot start", message, parent=dlg_root)
        dlg_root.destroy()
    except Exception:
        # Even the dialog failing must not mask the exit(1) that follows.
        log.exception("could not show fatal startup dialog")


# ---------------------------------------------------------------------- #
# Worker thread — the §3.5 loop                                           #
# ---------------------------------------------------------------------- #

def pipeline_worker(
    job_q: queue.Queue,
    ui_q: queue.Queue,
    stt: SttEngine,
    cleaner: Cleaner,
    state: AppState,
    restore_delay_ms: int = 300,   # config["paste_restore_delay_ms"]
    inject_mode: str = "paste",    # config["inject_mode"] — only "paste" exists yet
    history: History | None = None,  # §8.5 transcript history (None only in tests)
    dictionary: Dictionary | None = None,  # Chunk 6 (None only in tests)
    min_similarity: float = 0.45,  # config["min_correction_similarity"]
    fg_verdict=foreground_verdict,  # v2 U2: injectable for headless tests
    watcher_enabled: bool = True,        # config["correction_watcher_enabled"]
    watcher_factory=None,                # None -> real CorrectionWatcher; tests fake it
    watcher_poll_sec: float = 2.5,       # config["correction_watch_poll_sec"]
    watcher_duration_sec: float = 45.0,  # config["correction_watch_sec"]
    cancel_event: threading.Event | None = None,  # Esc-to-cancel flag (hook thread sets)
) -> None:
    """Single worker (deliberately not a pool — dictations are serial by
    nature, and one worker removes every model/clipboard/dictionary race by
    construction). Runs until it receives the None sentinel.

    v2 (U2): the "inject" job kind is retired — nothing enqueues it. A
    "dictate" job carries the whole pipeline through to the paste on this
    one thread. `fg_verdict` is the foreground precheck (the real
    injector.foreground_verdict by default); tests inject a lambda so the
    suite stays headless and clipboard-safe.

    v2 (U3): after a successful paste, _do_inject fire-and-forgets a
    correction watcher (see module docstring). `watcher` below is the
    loop-local supersession holder — the SINGLE place the previous
    watcher's cancel Event lives, so one live watcher exists at a time by
    construction. The watcher posts ("learned_corrections", [Correction,
    ...]) jobs back here; dictionary mutation stays on this one thread
    (single-writer invariant, plan §3.5)."""
    if watcher_factory is None:
        watcher_factory = CorrectionWatcher
    watcher = {"cancel": None} if watcher_enabled else None
    log.info("pipeline worker started")
    while True:
        job = job_q.get()
        if job is None:  # sentinel from shutdown
            log.info("pipeline worker got sentinel — exiting")
            return
        try:
            kind = job[0]
            if kind == "dictate":
                _handle_dictate(job, ui_q, stt, cleaner, state, dictionary,
                                restore_delay_ms=restore_delay_ms,
                                inject_mode=inject_mode, history=history,
                                fg_verdict=fg_verdict, job_q=job_q,
                                min_similarity=min_similarity,
                                watcher=watcher,
                                watcher_factory=watcher_factory,
                                watcher_poll_sec=watcher_poll_sec,
                                watcher_duration_sec=watcher_duration_sec,
                                cancel_event=cancel_event)
            elif kind == "learned_corrections":
                _handle_learned_corrections(job, ui_q, dictionary)
            elif kind == "teach":
                _handle_teach(job, ui_q, dictionary, min_similarity)
            elif kind in ("dict_toggle", "dict_delete"):
                _handle_dict_mutation(job, ui_q, dictionary)
            else:
                log.warning("unknown job kind: %r", kind)
        except Exception:
            # THE failure-mode contract (plan §6 Chunk 4): any worker
            # exception is caught here, logged with traceback, and the state
            # machine is force-reset so the hotkey works again immediately.
            log.exception("pipeline worker error on job kind=%r", job[0])
            state.force(State.IDLE)
            ui_q.put(("status", State.IDLE))
            ui_q.put(("toast", "error — see log"))


def _consume_cancel(cancel_event: threading.Event | None) -> bool:
    """Check-and-clear the Esc-to-cancel flag (worker thread only). The
    clear makes each Esc press cancel at most ONE dictation — a flag set
    too late to catch this one must not silently kill the next."""
    if cancel_event is not None and cancel_event.is_set():
        cancel_event.clear()
        return True
    return False


def _abort_dictation(state: AppState, from_: State, ui_q: queue.Queue,
                     history: History | None, text: str | None) -> None:
    """Common tail for an Esc-cancelled dictation (worker thread): whatever
    text already exists goes to History as "discarded" (recoverable — same
    outcome the old review popup's Esc used), toast, back to IDLE."""
    log.info("dictation cancelled by Esc during %s", from_.name)
    if history is not None and text:
        try:
            history.add(text, "discarded")
            ui_q.put(("app_refresh",))
        except Exception:
            log.exception("history record failed on cancel")
    ui_q.put(("toast", "cancelled — nothing pasted"))
    state.try_transition(from_, State.IDLE)
    ui_q.put(("status", State.IDLE))


def _handle_dictate(job, ui_q: queue.Queue, stt: SttEngine, cleaner: Cleaner,
                    state: AppState, dictionary: Dictionary | None = None, *,
                    restore_delay_ms: int = 300, inject_mode: str = "paste",
                    history: History | None = None,
                    fg_verdict=foreground_verdict,
                    job_q: queue.Queue | None = None,
                    min_similarity: float = 0.45,
                    watcher: dict | None = None,
                    watcher_factory=None,
                    watcher_poll_sec: float = 2.5,
                    watcher_duration_sec: float = 45.0,
                    cancel_event: threading.Event | None = None) -> None:
    _, audio, target_hwnd = job

    # Esc landed between the release and this job being picked up — nothing
    # transcribed yet, nothing to keep.
    if _consume_cancel(cancel_event):
        _abort_dictation(state, State.TRANSCRIBING, ui_q, history, None)
        return

    # --- Chunk 6: dictionary feeds BOTH pipeline layers ---
    # Cheap mtime stat first, so tray "Open dictionary" hand-edits take
    # effect on THIS dictation without a restart (plan §6 Chunk 6).
    stt_prompt = ""
    dictionary_block = ""
    if dictionary is not None:
        try:
            dictionary.reload_if_changed()
            stt_prompt = dictionary.stt_prompt()
            dictionary_block = dictionary.cleanup_context()
        except Exception:
            # A broken dictionary must never kill dictation — run unbiased.
            log.exception("dictionary prompts failed — dictating without bias")
            stt_prompt = dictionary_block = ""

    # --- STT (state already TRANSCRIBING, set by on_release) ---
    # Layer 1 of the feedback loop: bias Whisper's decoder toward the
    # canonicals ("Glossary: Wispr Flow, ..." — plan §5 stt_prompt).
    stt_res = stt.transcribe(audio, initial_prompt=stt_prompt or None)
    if not stt_res.text:
        # Silence / empty buffer: nothing to paste, straight back to IDLE.
        ui_q.put(("toast", "heard nothing"))
        state.try_transition(State.TRANSCRIBING, State.IDLE)
        ui_q.put(("status", State.IDLE))
        return

    # Esc landed while Whisper ran — the transcript exists, so it's
    # recoverable from History, but nothing goes further.
    if _consume_cancel(cancel_event):
        _abort_dictation(state, State.TRANSCRIBING, ui_q, history, stt_res.text)
        return

    # --- Claude cleanup ---
    state.try_transition(State.TRANSCRIBING, State.CLEANING)
    ui_q.put(("status", State.CLEANING))
    # Layer 2 of the feedback loop: Haiku rewrites any misheard variant
    # that slipped past the decoder bias (plan §5 cleanup_context).
    clean_res = cleaner.clean(stt_res.text, dictionary_block=dictionary_block)

    # v2: degraded output (regex fallback / raw) still pastes — it's usable
    # text and History has it either way. The toast is the visibility.
    if clean_res.degraded:
        ui_q.put(("toast", "raw mode — " + (clean_res.error or "Claude unavailable")))

    # Esc landed while Claude cleaned — cleaned text to History, no paste.
    if _consume_cancel(cancel_event):
        _abort_dictation(state, State.CLEANING, ui_q, history, clean_res.text)
        return

    # --- Auto-paste (v2 U2: no review stop — straight into injection on
    # this same worker thread; the old popup-ordering constraint is gone
    # and a separate "inject" job kind no longer earns its keep) ---
    state.try_transition(State.CLEANING, State.INJECTING)
    ui_q.put(("status", State.INJECTING))
    _do_inject(clean_res.text, target_hwnd, ui_q, state, restore_delay_ms,
               inject_mode, history, dictionary, fg_verdict=fg_verdict,
               job_q=job_q, min_similarity=min_similarity,
               watcher=watcher, watcher_factory=watcher_factory,
               watcher_poll_sec=watcher_poll_sec,
               watcher_duration_sec=watcher_duration_sec,
               cancel_event=cancel_event)


#: injector reason -> user-visible toast. None-reason success stays silent
#: (a toast per successful dictation would be noise). Every failure mode
#: gets a DISTINCT message (plan §6 Chunk 5, amended §8.5: the abort paths
#: no longer leave text on the clipboard — they point at History instead).
_INJECT_TOASTS = {
    "window_gone": "target window gone — saved to History (tray)",
    "focus": "couldn't refocus target — saved to History (tray)",
    "clipboard": "paste failed — clipboard locked by another app; saved to History (tray)",
    "snapshot": "pasted — couldn't save your previous clipboard",
    "restore_failed": "pasted — couldn't fully restore your previous clipboard (see log)",
}


def _inject_outcome(res) -> str:
    """Map an InjectResult to a History outcome (§8.5).

    ok=True (incl. snapshot/restore_failed — the PASTE landed) -> "pasted";
    the two no-payload aborts -> "no_target"; anything else -> "paste_failed".
    """
    if res.ok:
        return "pasted"
    if res.reason in ("window_gone", "focus"):
        return "no_target"
    return "paste_failed"


def _do_inject(text: str, target_hwnd: int, ui_q: queue.Queue,
               state: AppState, restore_delay_ms: int, inject_mode: str,
               history: History | None = None,
               dictionary: Dictionary | None = None,
               fg_verdict=foreground_verdict,
               job_q: queue.Queue | None = None,
               min_similarity: float = 0.45,
               watcher: dict | None = None,
               watcher_factory=None,
               watcher_poll_sec: float = 2.5,
               watcher_duration_sec: float = 45.0,
               cancel_event: threading.Event | None = None) -> None:
    """v2 auto-paste tail of the dictate pipeline (runs on the worker
    thread, called directly by _handle_dictate — state is already
    INJECTING): foreground precheck -> paste -> History -> learn -> IDLE.

    Foreground precheck (v2 U2, THE popup replacement): paste proceeds on
    verdict "same"/"none"/"own" — a transient toast/UAC focus grab or our
    own pill must never eat a dictation. On "other" (the user deliberately
    moved to a different app during the 3-6s pipeline): NO paste, NO
    SetForegroundWindow yank; the text is recorded to History with outcome
    "window_changed" and a toast points there.

    Learn step: dictionary.note_applied() on the pasted text (_learn with
    no before/after pair to diff — popup edits no longer exist; U4's teach
    path adds the other half). Runs AFTER the paste so learning latency
    never delays injection.

    Watcher spawn (v2 U3, only when the paste actually LANDED — res.ok):
    supersede the previous watcher (set its cancel Event, held in the
    worker-loop-local `watcher` dict), mint a fresh Event, and fire-and-
    forget a correction watcher daemon that observes the target control for
    user fixes and posts them back via job_q. Never joined; a skipped
    ("other" verdict) or failed paste spawns nothing — there is nothing on
    screen to watch. Guarded: a spawn bug must never turn a successful
    paste into the worker's "error — see log" path.

    Documented limitation: a paste that lands nowhere (target app has no
    paste handler) is undetectable — ok=True means "focus returned and
    Ctrl+V sent". The transcript is in History as the manual fallback (§8.5:
    the clipboard is restored, so it no longer holds the text).
    """
    # Last cancel window before the clipboard is touched (INJECTING covers
    # the paste + restore delay — after paste_into starts, it's too late).
    if _consume_cancel(cancel_event):
        _abort_dictation(state, State.INJECTING, ui_q, history, text)
        return

    verdict = fg_verdict(target_hwnd or 0)
    if verdict == "other":
        log.info("inject skipped: foreground changed (hwnd=0x%X) — "
                 "saved to History", target_hwnd or 0)
        if history is not None:
            try:
                history.add(text, "window_changed")
                ui_q.put(("app_refresh",))  # v2 U4: live-update an open window
            except Exception:
                log.exception("history record failed on window_changed")
        ui_q.put(("toast", "window changed — saved to History"))
        state.try_transition(State.INJECTING, State.IDLE)
        ui_q.put(("status", State.IDLE))
        return

    if inject_mode != "paste":
        # "type" keystroke fallback is planned but not built (plan §3.4);
        # the key is read from config now so it's wired for later.
        log.warning("inject_mode=%r not implemented yet — using paste", inject_mode)

    res = paste_into(target_hwnd or 0, text, restore_delay_ms)
    log.info(
        "inject result: ok=%s clipboard_restored=%s reason=%r "
        "(%d chars, hwnd=0x%X, fg=%s)",
        res.ok, res.clipboard_restored, res.reason, len(text),
        target_hwnd or 0, verdict,
    )

    # §8.5: every dictation is recorded, whatever happened to the paste.
    # Guarded so a history-file hiccup can't turn a successful paste into a
    # scary "error — see log" toast from the worker's catch-all.
    if history is not None:
        try:
            history.add(text, _inject_outcome(res))
            ui_q.put(("app_refresh",))  # v2 U4: live-update an open window
        except Exception:
            log.exception("history record failed (paste itself unaffected)")

    toast = _INJECT_TOASTS.get(res.reason or "")
    if toast is None and not res.ok:
        toast = "paste failed — see log"  # unknown reason: never fail silently
    if toast:
        ui_q.put(("toast", toast))

    # --- learn step (AFTER the paste — never delays injection). No
    # pre-edit text exists here, so this is note_applied-only; the diff
    # half now lives in the correction watcher below. ---
    if dictionary is not None:
        _learn(dictionary, None, text, 0.45, ui_q)

    # --- correction watcher (v2 U3) — only when the paste landed. ---
    if res.ok and watcher is not None and watcher_factory is not None \
            and job_q is not None:
        try:
            if watcher["cancel"] is not None:
                watcher["cancel"].set()  # supersede: at most one live watcher
            cancel = threading.Event()
            watcher["cancel"] = cancel
            watcher_factory(
                target_hwnd or 0, text, job_q, cancel,
                poll_sec=watcher_poll_sec,
                duration_sec=watcher_duration_sec,
                min_similarity=min_similarity,
            ).start()
            log.info("correction watcher started (hwnd=0x%X, %ds watch)",
                     target_hwnd or 0, int(watcher_duration_sec))
        except Exception:
            log.exception("correction watcher spawn failed (paste unaffected)")

    state.try_transition(State.INJECTING, State.IDLE)
    ui_q.put(("status", State.IDLE))


def _learn(dictionary: Dictionary, cleaned_before_edit: str | None,
           accepted_text: str, min_similarity: float,
           ui_q: queue.Queue) -> None:
    """Diff a before/after text pair; feed the dictionary (plan §6 Chunk 6
    step 5). v2 U2 interim: the review popup that produced the pair is
    gone, so _do_inject calls this with cleaned_before_edit=None — only the
    note_applied bump fires. Kept whole (not shrunk to the bump) because
    U3's UIA watcher and U4's teach path rewire the diff half.

    - Every extracted correction -> dictionary.add_correction() + a
      'Learned: "x" → "Y"' toast per pair. Learning is ALWAYS visible —
      a silently-learned bad pair would poison future dictations invisibly.
    - Every enabled canonical present in the accepted text gets
      times_applied/last_used bumped (an accept counts as an application —
      plan §5's prompt-slot ranking signal). This runs after add_correction,
      so a just-learned canonical immediately records its first application.
    - One save() at the end when anything changed (atomic tmp+replace).
    - Fully guarded: a learner bug can never turn a successful paste into
      the worker's "error — see log" path.
    """
    try:
        corrections = []
        if cleaned_before_edit and accepted_text:
            corrections = extract_corrections(
                cleaned_before_edit, accepted_text, min_similarity
            )
        for c in corrections:
            dictionary.add_correction(c.misheard, c.canonical)
            log.info("learned correction: %r -> %r", c.misheard, c.canonical)
            ui_q.put(("toast", f'Learned: "{c.misheard}" → "{c.canonical}"'))
        applied = dictionary.note_applied(accepted_text)
        if corrections or applied:
            dictionary.save()
            # v2 U4: keep an open app window's counters live after a normal
            # dictation, not just after teach/toggle/delete/watcher mutations.
            _post_dict_snapshot(ui_q, dictionary)
    except Exception:
        log.exception("learn step failed (paste itself unaffected)")


def _handle_learned_corrections(job, ui_q: queue.Queue,
                                dictionary: Dictionary | None) -> None:
    """v2 U3: apply corrections the UIA watcher observed the user making in
    the target app. job = ("learned_corrections", [Correction, ...]).

    Runs HERE, on the single pipeline worker — the watcher thread never
    touches the Dictionary (single-writer invariant, plan §3.5). Toast
    wording is identical to the Chunk-6 learn format so live muscle memory
    holds. Guarded like _learn: a learn bug never becomes the worker's
    "error — see log" path.
    """
    if dictionary is None:
        return
    try:
        corrections = job[1]
        if corrections:
            for c in corrections:
                dictionary.add_correction(c.misheard, c.canonical)
                log.info("watcher learned correction: %r -> %r",
                         c.misheard, c.canonical)
                ui_q.put(("toast", f'Learned: "{c.misheard}" → "{c.canonical}"'))
            dictionary.save()
    except Exception:
        log.exception("learned_corrections failed (dictation flow unaffected)")
    # v2 U4: any dict mutation ends with a snapshot so an open app window
    # sees what the watcher just learned.
    _post_dict_snapshot(ui_q, dictionary)


def _post_dict_snapshot(ui_q: queue.Queue, dictionary: Dictionary) -> None:
    """Post a ("dict_changed", deepcopy) snapshot for the app window's
    Dictionary tab (v2 U4). The deepcopy happens HERE on the worker (the
    single writer), so the UI never reads the live entries list
    concurrently with worker mutation — it only ever renders dead copies.
    """
    ui_q.put(("dict_changed", copy.deepcopy(dictionary.entries)))


def _handle_teach(job, ui_q: queue.Queue, dictionary: Dictionary | None,
                  min_similarity: float = 0.45) -> None:
    """v2 U4: the app window's "Save & teach" — the user edited a History
    transcript to show what the dictation SHOULD have said.
    job = ("teach", original_text, edited_text).

    Runs HERE, on the single pipeline worker: extract_corrections diffs the
    pair (same learner the review popup used, then the UIA watcher), each
    pair feeds dictionary.add_correction + a 'Learned:' toast, one atomic
    save. History itself stays immutable — the edit feeds the learner only.
    Guarded like _learn: a teach bug never becomes "error — see log".
    Always ends with a dict_changed snapshot so the window's Dictionary tab
    reflects whatever (possibly nothing) was learned.
    """
    if dictionary is None:
        return
    try:
        _, original, edited = job
        corrections = extract_corrections(original, edited, min_similarity)
        for c in corrections:
            dictionary.add_correction(c.misheard, c.canonical)
            log.info("teach learned correction: %r -> %r",
                     c.misheard, c.canonical)
            ui_q.put(("toast", f'Learned: "{c.misheard}" → "{c.canonical}"'))
        if corrections:
            dictionary.save()
        else:
            ui_q.put(("toast", "no corrections found in that edit"))
    except Exception:
        log.exception("teach job failed (dictation flow unaffected)")
    _post_dict_snapshot(ui_q, dictionary)


def _handle_dict_mutation(job, ui_q: queue.Queue,
                          dictionary: Dictionary | None) -> None:
    """v2 U4: the app window's per-entry controls, executed on the worker
    (single-writer invariant — the window itself never touches Dictionary).

        ("dict_toggle", canonical, enabled)  -> set_enabled + save
        ("dict_delete", canonical)           -> remove + save

    A missing canonical (e.g. deleted twice before the refresh landed) is a
    no-op — no save, no toast, just the snapshot so the UI reconverges.
    Guarded; always ends with a dict_changed snapshot.
    """
    if dictionary is None:
        return
    try:
        if job[0] == "dict_toggle":
            _, canonical, enabled = job
            if dictionary.set_enabled(canonical, bool(enabled)):
                dictionary.save()
                log.info("dictionary entry %r -> enabled=%s", canonical,
                         bool(enabled))
        else:  # dict_delete
            _, canonical = job
            if dictionary.remove(canonical):
                dictionary.save()
                log.info("dictionary entry %r deleted", canonical)
    except Exception:
        log.exception("%s job failed (dictation flow unaffected)", job[0])
    _post_dict_snapshot(ui_q, dictionary)


# ---------------------------------------------------------------------- #
# Hotkey dispatch — "hold" and "dual" dictation_mode (hook-thread callbacks
# + a main-thread watchdog for the cases a hold-mode release always caught)
# ---------------------------------------------------------------------- #

class DualModeState:
    """Cross-thread bookkeeping for dictation_mode "dual" (hold-to-talk +
    double-tap latch). Plain attributes, not lock-guarded — read/write of a
    bool or a float is atomic under the GIL, the same pattern already used
    for Recorder.overflowed, so this is safe to touch from the hook thread
    and the tk main thread without extra locking.

    latched: True once a double-tap has been confirmed — the recording
        persists with no key held; only a later stop-tap or the watchdog's
        overflow check ends it.
    pending_release_at: set by on_release when a release is QUICK ENOUGH
        that it might be the first half of a double-tap (dual mode only).
        The recording is deliberately left armed and the state left at
        RECORDING while this is set — resolved either by a confirming
        press (on_press) or, if none comes, by the watchdog timing out.
    """

    def __init__(self) -> None:
        self.latched = False
        self.pending_release_at: float | None = None


class DualModeContext:
    """Bundles what the dual-mode watchdog (poll_ui_queue tick) needs, so
    poll_ui_queue's signature doesn't grow one parameter per field."""

    __slots__ = ("state", "recorder", "dual", "listener", "job_q",
                "dictation_mode", "double_tap_sec", "max_recording_sec")

    def __init__(self, state: AppState, recorder: Recorder, dual: DualModeState,
                listener: HotkeyListener, job_q: queue.Queue,
                dictation_mode: str, double_tap_sec: float,
                max_recording_sec: int) -> None:
        self.state = state
        self.recorder = recorder
        self.dual = dual
        self.listener = listener
        self.job_q = job_q
        self.dictation_mode = dictation_mode
        self.double_tap_sec = double_tap_sec
        self.max_recording_sec = max_recording_sec


def _stop_and_dispatch(state: AppState, recorder: Recorder, job_q: queue.Queue,
                       ui_q: queue.Queue, max_recording_sec: int) -> bool:
    """Disarm a latched (hands-free) recording and enqueue it for
    transcription, if the RECORDING->TRANSCRIBING CAS succeeds.

    Shared by the stop-tap (hook thread, _on_hotkey_press) and the overflow
    auto-stop (main thread, _dual_mode_watchdog_tick) — both run the exact
    disarm/hwnd-capture/CAS/enqueue sequence already proven hook-thread-safe
    by the original hold-mode on_release. Returns True iff a dictate job
    was enqueued, so the caller knows ITS attempt won the race (a
    concurrent caller loses the CAS and must not also clear the latch)."""
    audio = recorder.disarm_and_get()
    target_hwnd = win32gui.GetForegroundWindow()
    if not state.try_transition(State.RECORDING, State.TRANSCRIBING):
        return False
    if recorder.overflowed:
        ui_q.put(("toast", f"hit {max_recording_sec}s limit — processing what was captured"))
    job_q.put(("dictate", audio, target_hwnd))
    ui_q.put(("status", State.TRANSCRIBING))
    return True


def _resolve_pending_release(state: AppState, recorder: Recorder,
                             dual: DualModeState, listener: HotkeyListener,
                             job_q: queue.Queue, ui_q: queue.Queue,
                             max_recording_sec: int) -> None:
    """Main-thread timeout resolution for a dual-mode quick release that
    never got a confirming second press within double_tap_sec (see
    _dual_mode_watchdog_tick). Runs the same debounce-discard-or-transcribe
    finalize on_release would have run immediately in hold mode, using the
    ORIGINAL press-to-release duration for the DEBOUNCE_SEC check — never
    the extra time spent waiting to see if a double-tap was coming."""
    release_at = dual.pending_release_at
    dual.pending_release_at = None
    ts = listener.press_timestamp
    held = (release_at - ts) if (ts is not None and release_at is not None) else 0.0

    audio = recorder.disarm_and_get()
    target_hwnd = win32gui.GetForegroundWindow()
    if held < DEBOUNCE_SEC:
        if state.try_transition(State.RECORDING, State.IDLE):
            ui_q.put(("status", State.IDLE))
        return
    if not state.try_transition(State.RECORDING, State.TRANSCRIBING):
        return
    if recorder.overflowed:
        ui_q.put(("toast", f"hit {max_recording_sec}s limit — processing what was captured"))
    job_q.put(("dictate", audio, target_hwnd))
    ui_q.put(("status", State.TRANSCRIBING))


#: Bias the boundary race between a confirming press and the watchdog's
#: timeout toward the press winning (see _dual_mode_watchdog_tick).
_RESOLVE_GRACE_SEC = 0.05


def _dual_mode_watchdog_tick(state: AppState, recorder: Recorder,
                             dual: DualModeState, listener: HotkeyListener,
                             job_q: queue.Queue, ui_q: queue.Queue,
                             double_tap_sec: float, max_recording_sec: int,
                             now: float | None = None) -> None:
    """Called every poll_ui_queue tick (~50ms, main thread) when
    dictation_mode == "dual". Resolves the two things a hold-mode release
    always used to catch, which a latched/deferred recording has no
    release event to trigger (plan §5.5):

    1. Auto-stop an ACTIVE latch once recorder.overflowed fires at
       max_recording_sec — otherwise a hands-free recording left running
       (e.g. the user walked away) just keeps recording forever.
    2. Resolve a quick release still waiting to see if a second press would
       confirm a double-tap, once double_tap_sec has passed with no
       confirmation — otherwise a lone quick tap would stay armed forever.

    ``now`` is injectable for tests; production callers leave it None and
    get real wall-clock time.
    """
    if now is None:
        now = time.perf_counter()
    if dual.latched and recorder.overflowed:
        if _stop_and_dispatch(state, recorder, job_q, ui_q, max_recording_sec):
            dual.latched = False
        return

    if dual.pending_release_at is not None:
        elapsed = now - dual.pending_release_at
        if elapsed >= double_tap_sec + _RESOLVE_GRACE_SEC:
            _resolve_pending_release(state, recorder, dual, listener,
                                     job_q, ui_q, max_recording_sec)


def _on_cancel_press(state: AppState, recorder: Recorder, dual: DualModeState,
                     cancel_event: threading.Event,
                     ui_q: queue.Queue) -> bool:
    """Hook-thread Esc handler (sub-ms budget: a CAS, in-memory disarm, and
    queue puts). Returns True when the press was CONSUMED — the listener
    then suppresses the Esc system-wide so the abort doesn't also close
    something in the target app; an idle Esc returns False and passes
    through untouched.

    RECORDING (held, latched, or a dual-mode pending release): the CAS to
    IDLE claims the recording — whoever loses the race (this handler vs a
    concurrent watchdog finalize) simply does nothing — then the audio is
    discarded and the dual bookkeeping cleared so the watchdog goes inert.

    TRANSCRIBING/CLEANING/INJECTING: flag cancel_event; the worker drops
    the dictation at its next stage boundary (see _abort_dictation). If the
    RECORDING CAS above lost to a finalize that just enqueued the job, the
    state reads TRANSCRIBING here and the flag still kills that job — Esc
    wins the race either way.
    """
    if state.try_transition(State.RECORDING, State.IDLE):
        recorder.disarm_and_get()  # discard — Esc means "never mind"
        dual.latched = False
        dual.pending_release_at = None
        ui_q.put(("status", State.IDLE))
        ui_q.put(("toast", "cancelled — recording discarded"))
        return True
    if state.state in (State.TRANSCRIBING, State.CLEANING, State.INJECTING):
        cancel_event.set()
        return True
    return False


def _on_hotkey_press(state: AppState, recorder: Recorder, dual: DualModeState,
                     tap_tracker: DoubleTapTracker, paused: threading.Event,
                     job_q: queue.Queue, ui_q: queue.Queue,
                     dictation_mode: str, max_recording_sec: int,
                     now: float | None = None,
                     cancel_event: threading.Event | None = None) -> None:
    """Hook-thread callback (sub-ms budget — see hotkey.py module docstring).

    dictation_mode == "hold" reproduces the original single-gesture
    behavior: tap_tracker/dual never end up changing the outcome (dual.latched
    can only become True in "dual" mode below), so every branch here falls
    through to the same IDLE->RECORDING CAS / busy-beep as before.

    dictation_mode == "dual" adds two branches on top of that:

    - A press while a latch is active STOPS it, regardless of pause —
      symmetric with hold-mode's on_release, which isn't pause-gated
      either (only STARTING a new recording is blocked by pause).
    - A press that arrives inside double_tap_sec of a still-unresolved
      quick release CONFIRMS a double-tap: the recording (never disarmed
      across that release — see _on_hotkey_release) simply continues, now
      hands-free.
    """
    if state.state is State.RECORDING and dual.latched:
        if _stop_and_dispatch(state, recorder, job_q, ui_q, max_recording_sec):
            dual.latched = False
        return

    if paused.is_set():
        return

    if now is None:
        now = time.perf_counter()
    is_double_tap = dictation_mode == "dual" and tap_tracker.press(now)

    if state.state is State.RECORDING and dual.pending_release_at is not None:
        if is_double_tap:
            dual.pending_release_at = None
            dual.latched = True
        return  # confirmed (now latched) or a stray extra press — either way

    if not state.try_transition(State.IDLE, State.RECORDING):
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        return
    recorder.arm()
    # A cancel flag set too late to catch the PREVIOUS dictation (its
    # pipeline already finished) must never kill this fresh one.
    if cancel_event is not None:
        cancel_event.clear()
    dual.latched = is_double_tap
    ui_q.put(("status", State.RECORDING))


def _on_hotkey_release(state: AppState, recorder: Recorder, dual: DualModeState,
                       listener: HotkeyListener, job_q: queue.Queue,
                       ui_q: queue.Queue, dictation_mode: str,
                       double_tap_sec: float, max_recording_sec: int,
                       now: float | None = None) -> None:
    """Hook-thread callback (sub-ms budget).

    dictation_mode == "hold" reproduces the original release logic exactly
    (disarm_and_get() unconditionally first, then the DEBOUNCE_SEC check on
    the result — Chunk-1 finding, unchanged).

    dictation_mode == "dual" adds: a no-op while latched (the recording
    persists with no key held), and a state-guard for the release trailing
    a stop-tap that _on_hotkey_press already handled (without it, this
    release would misread its OWN key-up as a fresh ambiguous tap and set
    a stale pending_release_at — the guard is a no-op in hold mode, since a
    release only ever finds state != RECORDING there in the already-inert
    "release trailing a busy-beeped press" case). A release quick enough
    that it MIGHT be the first half of a double-tap is deferred — left
    armed, state left at RECORDING — for a confirming press or the
    watchdog to resolve (_on_hotkey_press / _dual_mode_watchdog_tick).
    """
    if dual.latched:
        return
    if state.state is not State.RECORDING:
        return

    if now is None:
        now = time.perf_counter()
    ts = listener.press_timestamp
    held = now - ts if ts is not None else 0.0

    if dictation_mode == "dual" and held < double_tap_sec \
            and dual.pending_release_at is None:
        dual.pending_release_at = now
        return

    # Definitive finalize — hold-mode's original logic, unchanged.
    audio = recorder.disarm_and_get()
    target_hwnd = win32gui.GetForegroundWindow()
    if held < DEBOUNCE_SEC:
        if state.try_transition(State.RECORDING, State.IDLE):
            ui_q.put(("status", State.IDLE))
        return
    if not state.try_transition(State.RECORDING, State.TRANSCRIBING):
        return
    if recorder.overflowed:
        ui_q.put(("toast", f"hit {max_recording_sec}s limit — processing what was captured"))
    job_q.put(("dictate", audio, target_hwnd))
    ui_q.put(("status", State.TRANSCRIBING))


# ---------------------------------------------------------------------- #
# Main thread — ui_q poll loop (root.after, never touched by other threads)
# ---------------------------------------------------------------------- #

def poll_ui_queue(root: tk.Tk, ui_q: queue.Queue,
                  tray: Tray, indicator: StatusIndicator,
                  app_window: AppWindow,
                  toggle_pause=None,
                  dual_ctx: DualModeContext | None = None) -> None:
    """Drain ui_q without blocking, then re-arm. 50ms is imperceptible.
    (The queue-plus-poll pattern is the documented-thread-safe one; calling
    root.after from the worker is not guaranteed safe — plan §3.5.)

    dual_ctx (only set when dictation_mode == "dual"): resolves the
    latched-overflow and pending-release-timeout watchdogs every tick,
    before draining ui_q (see _dual_mode_watchdog_tick)."""
    if dual_ctx is not None:
        _dual_mode_watchdog_tick(
            dual_ctx.state, dual_ctx.recorder, dual_ctx.dual, dual_ctx.listener,
            dual_ctx.job_q, ui_q, dual_ctx.double_tap_sec, dual_ctx.max_recording_sec,
        )
    try:
        while True:
            msg = ui_q.get_nowait()
            kind = msg[0]
            if kind == "status":
                tray.set_status(msg[1])
                indicator.set_status(msg[1])  # tray + pill track together
            elif kind == "toast":
                tray.toast(msg[1])
            elif kind == "open_app":
                # v2 U4: tray "Open Murmur" (also the icon's default
                # action). The tray callback is a queue-put on the pystray
                # thread; the ctk window is only ever touched HERE, on the
                # main thread.
                app_window.show()
            elif kind == "dict_changed":
                # v2 U4: the worker finished a dictionary mutation and
                # posted a deepcopy snapshot — the window renders copies
                # only, never the worker's live entries list.
                app_window.refresh_dictionary(msg[1])
            elif kind == "app_refresh":
                # v2 U4: History gained an entry; live-update the
                # transcripts list if the window is open (no-op otherwise).
                app_window.refresh_if_visible()
            elif kind == "pause_toggle":
                # Chunk 7: tray Pause/Resume. The pystray callback is a
                # queue-put; the actual stream close/reopen (PortAudio
                # calls) runs HERE on the main thread, never on the pystray
                # or hook threads.
                if toggle_pause is not None:
                    toggle_pause()
            elif kind == "quit":
                root.quit()  # ends mainloop; main() runs the shutdown block
                return       # do NOT re-arm the poll
            else:
                log.warning("unknown ui message kind: %r", kind)
    except queue.Empty:
        pass
    root.after(50, poll_ui_queue, root, ui_q, tray, indicator,
               app_window, toggle_pause, dual_ctx)


# ---------------------------------------------------------------------- #
# Entrypoint                                                               #
# ---------------------------------------------------------------------- #

def main() -> None:
    configure_logging(LOG_PATH)  # Chunk 7: rotating 1MB x3; console only if one exists
    _set_windows_app_identity()  # before ANY window — taskbar shows our icon
    config = load_config()

    # v2 launch-UX: "--show" means "bring the app window up." The Start Menu /
    # Windows Search shortcut passes it; the login Startup shortcut does NOT
    # (it just starts the background tray app silently).
    want_show = "--show" in sys.argv[1:]

    # --- preflight startup checks (Chunk 7 — FIRST, before any window or
    # thread exists): single-instance mutex, API key, input device, model
    # cache. First failure -> blocking dialog with the exact fix + exit(1).
    checks = run_startup_checks(config)
    if not checks.ok:
        if checks.already_running:
            # A second launch (e.g. from Search): don't nag with a dialog —
            # tell the running instance to open its window, then exit quietly.
            signal_show_window()
            log.info("already running — signalled show-window and exiting")
            sys.exit(0)
        log.error("startup check failed: %s", checks.fatal_message)
        _fatal_dialog(checks.fatal_message or "startup check failed — see log")
        sys.exit(1)

    # Presence was verified by check (2); this just reads the value.
    api_key = get_anthropic_key()

    state = AppState()
    job_q: queue.Queue = queue.Queue()
    ui_q: queue.Queue = queue.Queue()
    # Esc-to-cancel (2026-07-10): SET on the hook thread (_on_cancel_press),
    # CHECK-AND-CLEARED on the worker at stage boundaries, cleared on each
    # new recording — Event ops are lock-free enough for the hook budget.
    cancel_event = threading.Event()

    # --- audio stream: opened ONCE here, never on the hotkey path (§3.5) ---
    recorder = Recorder(
        sample_rate=config["sample_rate"], max_sec=config["max_recording_sec"]
    )
    try:
        recorder.open_stream()
    except Exception as e:
        # query_devices() passed in the startup checks, but the device can
        # still refuse to OPEN (driver/exclusive-mode issues). Dialog, not a
        # stderr print — under pythonw sys.stderr is None and invisible.
        log.exception("audio stream failed to open")
        _fatal_dialog(f"Microphone exists but its stream failed to open ({e}) — "
                      "check no other app holds it exclusively, then restart.")
        sys.exit(1)

    stt = SttEngine(
        model_size=config["model_size"],
        compute_type=config["compute_type"],
        cpu_threads=config["cpu_threads"],
        cache_dir=PROJECT_ROOT / config["model_cache_dir"],
    )
    cleaner = Cleaner(
        api_key=api_key,
        model=config["claude_model"],
        timeout_sec=config["claude_timeout_sec"],
    )

    # --- tk root (withdrawn — the popup is the only visible window) ---
    root = tk.Tk()
    root.withdraw()
    # Default icon for every toplevel this root ever spawns (the app window
    # re-asserts its own copy anyway; this catches everything else).
    if APP_ICON_PATH.exists():
        try:
            root.iconbitmap(default=str(APP_ICON_PATH))
        except Exception:
            log.debug("could not set default window icon", exc_info=True)

    # --- Chunk 7: real tray Pause/Resume (plan §3.5 mitigation) ---
    # `paused` is a threading.Event: SET/CLEARED only on the main thread
    # (via do_toggle_pause below), but READ from the pynput hook thread in
    # on_press — Event.is_set() is a lock-free flag read, well within the
    # hook's sub-ms budget.
    paused = threading.Event()

    def do_toggle_pause() -> None:
        """Runs on the MAIN thread (via the ("pause_toggle",) ui_q message).

        Pause: block new recordings FIRST (flag), then close the stream —
        the Windows mic-in-use indicator goes off (the §3.5 trade-off's
        opt-out). A dictation already in flight (recording/transcribing/
        cleaning) finishes normally; pausing mid-hold truncates that one
        recording at the pause click but still processes what was captured.

        Resume: reopen the stream; if the device disappeared since Pause,
        toast and STAY paused — never crash, and the tray keeps saying
        "Resume" because set_paused() is only called on success.
        """
        if not paused.is_set():
            paused.set()
            recorder.close_stream()
            tray.set_paused(True)
            app_window.set_paused(True)  # v2 U4: footer mirrors the tray
            tray.toast(f"paused — mic released; {config['hotkey'].upper()} "
                       "ignored until Resume")
            log.info("paused (mic stream closed)")
        else:
            try:
                recorder.open_stream()
            except Exception:
                log.exception("resume failed — audio stream would not reopen")
                tray.toast("resume FAILED — no usable microphone; still paused")
                return  # stay paused; tray label/icon unchanged
            paused.clear()
            tray.set_paused(False)
            app_window.set_paused(False)  # v2 U4: footer mirrors the tray
            tray.toast("resumed — mic live")
            log.info("resumed (mic stream reopened)")

    # --- tray (menu callbacks run on the pystray thread: queue-puts only) ---
    def on_toggle_pause() -> None:
        # Queue-put only — the stream close/reopen happens on the main
        # thread in do_toggle_pause (PortAudio calls are not for this thread).
        ui_q.put(("pause_toggle",))

    def on_open_dictionary() -> None:
        import os
        try:
            os.startfile(DICTIONARY_PATH)
        except OSError:
            log.exception("could not open dictionary.json")

    tray = Tray(
        on_toggle_pause=on_toggle_pause,
        on_open_dictionary=on_open_dictionary,
        # v2 U4: queue-put only — the app window is a ctk window and must
        # be touched exclusively from the main thread's poll_ui_queue.
        on_open_app=lambda: ui_q.put(("open_app",)),
        on_quit=lambda: ui_q.put(("quit",)),
    )
    tray.start()

    # --- model load (blocking, once, with splash toast) ---
    tray.toast(f"loading {config['model_size']} model…")
    try:
        load_sec = stt.load()
    except Exception:
        log.exception("model load failed")
        tray.toast("model load failed — see log (first run needs internet once)")
        time.sleep(2)  # let the toast render before the process dies
        recorder.close_stream()
        tray.stop()
        sys.exit(1)

    # --- transcript history (§8.5, v2) — all writes happen on the worker
    # thread (every dictation outcome, incl. "window_changed"); History's
    # internal lock serializes them with the viewer's main-thread reads.
    history = History(HISTORY_PATH)

    # --- self-correcting dictionary (Chunk 6, plan §5) — quarantines a
    # corrupt file and starts fresh rather than ever refusing to launch.
    # All mutation happens on the single worker thread; the dictate path
    # re-reads on mtime change so tray hand-edits apply next dictation.
    dictionary = Dictionary.load(
        DICTIONARY_PATH,
        autoenable=config["learned_entry_autoenable"],
        max_prompt_entries=config["max_dictionary_prompt_entries"],
    )

    # --- worker thread ---
    worker = threading.Thread(
        target=pipeline_worker,
        args=(job_q, ui_q, stt, cleaner, state,
              config["paste_restore_delay_ms"], config["inject_mode"],
              history, dictionary, config["min_correction_similarity"]),
        kwargs={
            # v2 U3: the UIA correction watcher (enabled=False -> never
            # spawned; the pipeline is byte-identical to pre-U3 then).
            "watcher_enabled": config["correction_watcher_enabled"],
            "watcher_poll_sec": config["correction_watch_poll_sec"],
            "watcher_duration_sec": config["correction_watch_sec"],
            "cancel_event": cancel_event,
        },
        name="pipeline",
        daemon=True,
    )
    # v2 U4: seed the app window's Dictionary tab with a startup snapshot —
    # deepcopied BEFORE the worker starts (no mutation can be in flight),
    # posted once; every later snapshot comes from the worker itself.
    ui_q.put(("dict_changed", copy.deepcopy(dictionary.entries)))
    worker.start()

    # --- floating status pill (visible twin of the tray dot) ---
    indicator = StatusIndicator(root, level_provider=lambda: recorder.level)

    # --- app window (v2 U4) — withdrawn CTkToplevel over the shared root,
    # shown via the ("open_app",) ui_q message (tray "Open Murmur" /
    # icon double-click). Reads History directly (locked); mutates the
    # dictionary ONLY by posting jobs (job_q.put) to the single worker.
    app_window = AppWindow(root, history, job_q.put, config)

    # v2 launch-UX: a later launch from Search signals us to surface the
    # window. The listener runs on a daemon thread; on_show just enqueues
    # ("open_app",) so the actual show() happens on the tk main thread via
    # the poll loop (never touch tk off-thread).
    start_show_window_listener(lambda: ui_q.put(("open_app",)))
    # If THIS launch asked to show (Search-launched when nothing was running
    # yet, so we became the primary), open the window once we're up.
    if want_show:
        ui_q.put(("open_app",))

    # --- hotkey callbacks (HOOK THREAD — sub-ms budget, nothing blocking) ---
    # dictation_mode "hold" = original single-gesture behavior, unchanged.
    # "dual" adds a double-tap latch on top: double-tap to go hands-free,
    # tap again to stop, while a plain hold still works too (see
    # _on_hotkey_press/_on_hotkey_release + the DualModeState/watchdog
    # section above pipeline_worker).
    dictation_mode = config["dictation_mode"]
    double_tap_sec = config["double_tap_sec"]
    dual = DualModeState()
    tap_tracker = DoubleTapTracker(double_tap_sec)

    listener: HotkeyListener  # assigned below; closures resolve at call time

    def on_press() -> None:
        _on_hotkey_press(state, recorder, dual, tap_tracker, paused, job_q,
                         ui_q, dictation_mode, config["max_recording_sec"],
                         cancel_event=cancel_event)

    def on_release() -> None:
        _on_hotkey_release(state, recorder, dual, listener, job_q, ui_q,
                           dictation_mode, double_tap_sec,
                           config["max_recording_sec"])

    def on_cancel() -> bool:
        # Esc: cancel whatever is in flight; True (= consumed, suppress the
        # keystroke) only when something actually got cancelled.
        return _on_cancel_press(state, recorder, dual, cancel_event, ui_q)

    listener = HotkeyListener(
        key_name=config["hotkey"],
        suppress=config["suppress_hotkey"],
        on_press=on_press,
        on_release=on_release,
        on_cancel=on_cancel,
    )
    listener.start()

    # Only "dual" needs the watchdog tick (latched-overflow auto-stop +
    # pending-release timeout) — poll_ui_queue no-ops on dual_ctx=None.
    dual_ctx = DualModeContext(
        state, recorder, dual, listener, job_q,
        dictation_mode, double_tap_sec, config["max_recording_sec"],
    ) if dictation_mode == "dual" else None

    # --- go ---
    root.after(50, poll_ui_queue, root, ui_q, tray, indicator,
               app_window, do_toggle_pause, dual_ctx)
    tray.set_status(State.IDLE)
    indicator.set_status(State.IDLE)  # starts hidden; explicit for symmetry
    if dictation_mode == "dual":
        tray.toast(
            f"Murmur ready — hold {config['hotkey'].upper()} to dictate, or "
            f"double-tap it to go hands-free; Esc cancels "
            f"(model loaded in {load_sec:.1f}s)"
        )
    else:
        tray.toast(
            f"Murmur ready — hold {config['hotkey'].upper()} to dictate, "
            f"Esc cancels (model loaded in {load_sec:.1f}s)"
        )
    log.info("flow-clone ready (hotkey=%s, model=%s, dictation_mode=%s)",
             config["hotkey"], config["model_size"], dictation_mode)

    root.mainloop()

    # --- shutdown (reached via tray Quit -> ("quit",) -> root.quit()) ---
    log.info("shutting down")
    listener.stop()
    job_q.put(None)            # sentinel — wakes/ends the worker loop
    worker.join(timeout=15)    # may be mid-STT/API; daemon thread dies at exit anyway
    if worker.is_alive():
        log.warning("worker did not exit within 15s — proceeding (daemon)")
    recorder.close_stream()
    tray.stop()
    root.destroy()
    log.info("clean exit")


if __name__ == "__main__":
    main()
