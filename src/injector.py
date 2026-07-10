"""Text injection for flow-clone (Chunk 5, amended per plan §8.5):
full best-effort binary clipboard snapshot/restore -> paste -> restore,
plus foreground-window focus return.

WHY THE §8.5 AMENDMENT EXISTS — Gate 2 live testing found the original
§3.4 policy ("any non-text format on the clipboard => don't restore")
destroyed the user's clipboard on virtually every dictation: text copied
from modern apps (Chrome, Word, VS Code, Slack) always carries
"HTML Format" / "Rich Text Format" ALONGSIDE CF_UNICODETEXT, so plain
copied text was wrongly classified non-restorable. Locked-in replacement:

- Save: OpenClipboard with a 5x20ms retry loop (another process can hold
  the clipboard lock), then enumerate ALL formats and capture each one:
  CF_UNICODETEXT as str, CF_HDROP as the file-path tuple, every other
  format as raw bytes where pywin32 hands us bytes. Skipped formats (see
  _SKIP_FORMATS): handle-based GDI formats that can't round-trip as bytes,
  and synthesized formats Windows regenerates automatically from what we
  DO capture. Per-format read failures (a delayed-render source refusing
  to render) are logged and skipped — never fatal.
- Inject: EmptyClipboard -> SetClipboardData(CF_UNICODETEXT, text) ->
  close -> send Ctrl+V via pynput.keyboard.Controller -> sleep
  paste_restore_delay_ms so the target app reads the clipboard before we
  touch it again.
- Restore: EmptyClipboard, then write back EVERY captured format — text
  via CF_UNICODETEXT, files via a hand-packed DROPFILES struct for
  CF_HDROP, everything else as bytes. Per-format write failures are
  logged; True only if every captured format made it back. The only
  unrestorable case left is a clipboard that was PURE delayed-render and
  refused to render at snapshot time — rare, and reported honestly via
  the "snapshot"/"restore_failed" reasons.

NO CONSOLATION CLIPBOARD (killed per §8.5): on the window-gone /
focus-failure abort paths the paste payload has not been written yet, so
the clipboard is simply left EXACTLY as found. The transcript's safety
net is the History file instead (main.py records every accepted
transcript with its outcome; tray -> History copies it back out).

Focus return (plan §6 Chunk 5, still needed in v2): focus can wander
during the pipeline (a toast, our own pill, a transient dialog), so before
pasting we hand it back to the hwnd captured at hotkey-release time.
``SetForegroundWindow`` is subject to Windows' foreground-lock rule — a
background process may not steal foreground unless it recently received
input. The classic satisfier: synthesize a press+release of VK_MENU (Alt)
via pynput, which credits this process with input, then retry once.

Documented limitation (plan §6 Chunk 5): a paste that lands nowhere (target
app has no paste handler, terminals that remap paste, ...) is undetectable
from here — ``ok=True`` means "focus returned and Ctrl+V was sent", not
"characters visibly appeared". The transcript is in History as the fallback
(the clipboard is restored, so it no longer holds the text).

Threading: every function here is called from the pipeline worker thread
only (single worker => no two paste_into calls can ever race over the
clipboard, plan §3.5) — except set_clipboard_text, which the History
viewer also calls on the tk main thread for its user-initiated,
deliberately clipboard-overwriting Copy action. Nothing here touches
tkinter.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Sequence, Union

import pywintypes
import win32clipboard
import win32con
import win32gui
import win32process
from pynput import keyboard

log = logging.getLogger(__name__)

#: OpenClipboard retry policy (plan §3.4): 5 attempts, 20ms apart.
_OPEN_RETRIES = 5
_OPEN_RETRY_DELAY_SEC = 0.020

#: After SetForegroundWindow, give Windows a beat to actually move focus
#: before we trust GetForegroundWindow as the verdict.
_FOCUS_SETTLE_SEC = 0.05

#: Formats deliberately NOT captured by snapshot_clipboard (§8.5):
#:
#: Synthesized text family — Windows regenerates these automatically from
#: the CF_UNICODETEXT we DO capture, so re-capturing them is redundant and
#: re-setting them can fight the synthesizer:
#:   CF_TEXT(1), CF_OEMTEXT(7), CF_LOCALE(16)
#: Handle-based GDI formats — pywin32 returns handles, not bytes; a copied
#: handle doesn't survive EmptyClipboard, so they can't round-trip this
#: way. Windows re-synthesizes CF_BITMAP from a restored CF_DIB anyway:
#:   CF_BITMAP(2), CF_METAFILEPICT(3), CF_PALETTE(9), CF_ENHMETAFILE(14)
#: Owner-display / private-display twins — meaningless without the
#: original owning window:
#:   CF_OWNERDISPLAY(0x80), CF_DSPTEXT(0x81), CF_DSPBITMAP(0x82),
#:   CF_DSPMETAFILEPICT(0x83), CF_DSPENHMETAFILE(0x8E)
_SKIP_FORMATS = frozenset({
    win32con.CF_TEXT,
    win32con.CF_OEMTEXT,
    win32con.CF_LOCALE,
    win32con.CF_BITMAP,
    win32con.CF_METAFILEPICT,
    win32con.CF_PALETTE,
    win32con.CF_ENHMETAFILE,
    win32con.CF_OWNERDISPLAY,
    win32con.CF_DSPTEXT,
    win32con.CF_DSPBITMAP,
    win32con.CF_DSPMETAFILEPICT,
    win32con.CF_DSPENHMETAFILE,
})

#: What a captured clipboard format's data can be:
#: str for CF_UNICODETEXT, tuple[str, ...] for CF_HDROP, bytes otherwise.
FormatData = Union[str, tuple, bytes]


class ClipboardLockedError(Exception):
    """OpenClipboard still failing after the full retry loop."""


@dataclass
class ClipboardSnapshot:
    """Best-effort binary snapshot of the whole clipboard (§8.5).

    ``formats`` maps clipboard format id -> captured data:
      CF_UNICODETEXT -> str
      CF_HDROP       -> tuple of file paths (as pywin32 returns it)
      anything else  -> raw bytes
    An empty dict is a valid snapshot of an empty clipboard (restore
    empties the clipboard back).
    """
    formats: dict[int, FormatData] = field(default_factory=dict)

    @property
    def text(self) -> str | None:
        """Convenience: the CF_UNICODETEXT payload, if one was captured."""
        val = self.formats.get(win32con.CF_UNICODETEXT)
        return val if isinstance(val, str) else None


@dataclass
class InjectResult:
    ok: bool                    # focus returned AND Ctrl+V sent
    clipboard_restored: bool    # the user's pre-paste clipboard is intact
                                # (faithfully restored, or never touched on
                                # the abort paths)
    reason: str | None = None   # why not-ok / why not-restored (see paste_into)


# ---------------------------------------------------------------------- #
# Clipboard primitives                                                    #
# ---------------------------------------------------------------------- #

def _open_clipboard_with_retry() -> None:
    """OpenClipboard, retrying 5x20ms — another process may hold the lock."""
    last_err: pywintypes.error | None = None
    for attempt in range(_OPEN_RETRIES):
        try:
            win32clipboard.OpenClipboard(0)
            return
        except pywintypes.error as e:
            last_err = e
            log.debug("OpenClipboard attempt %d/%d failed: %s",
                      attempt + 1, _OPEN_RETRIES, e)
            time.sleep(_OPEN_RETRY_DELAY_SEC)
    raise ClipboardLockedError(
        f"clipboard still locked after {_OPEN_RETRIES} attempts: {last_err}"
    ) from last_err


def _format_name(fmt: int) -> str:
    """Human-readable format id for logs ("49407 (HTML Format)" / "13")."""
    try:
        return f"{fmt} ({win32clipboard.GetClipboardFormatName(fmt)})"
    except pywintypes.error:
        return str(fmt)  # standard formats have no registered name


def _pack_dropfiles(paths: Sequence[str]) -> bytes:
    """Build a CF_HDROP payload: DROPFILES header + double-null-terminated
    UTF-16-LE path list (§8.5).

    DROPFILES (shlobj_core.h) is 20 bytes:
        DWORD pFiles  = 20   offset of the path list = sizeof(DROPFILES)
        POINT pt      = 0,0  two LONGs (drop coordinates; unused here)
        BOOL  fNC     = 0
        BOOL  fWide   = 1    paths are wide chars (UTF-16-LE)
    Then each path UTF-16-LE + its own null terminator, and one extra
    double-null closing the whole list.
    """
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    body = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in paths)
    return header + body + b"\x00\x00"


def snapshot_clipboard() -> ClipboardSnapshot:
    """Capture every restorable clipboard format for later restore (§8.5).

    Per-format read failures are logged and skipped — never fatal. Raises
    ClipboardLockedError only if the clipboard can't be opened at all; the
    caller (paste_into) proceeds without a snapshot in that case.
    """
    _open_clipboard_with_retry()
    formats: dict[int, FormatData] = {}
    try:
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            if fmt in _SKIP_FORMATS:
                continue
            try:
                data = win32clipboard.GetClipboardData(fmt)
            except (pywintypes.error, TypeError) as e:
                # Delayed-render source died / refused, or a format pywin32
                # can't read as data. Skip it — never fatal (§8.5).
                log.warning("snapshot: format %s unreadable, skipped: %s",
                            _format_name(fmt), e)
                continue
            if fmt == win32con.CF_UNICODETEXT:
                if isinstance(data, str):
                    formats[fmt] = data
            elif fmt == win32con.CF_HDROP:
                if isinstance(data, tuple):
                    formats[fmt] = data
            elif isinstance(data, bytes):
                formats[fmt] = data
            else:
                # pywin32 returned a handle/int/other for a format outside
                # our skip list — can't round-trip it as bytes; skip.
                log.info("snapshot: format %s returned %s, not bytes — skipped",
                         _format_name(fmt), type(data).__name__)
    finally:
        win32clipboard.CloseClipboard()
    log.debug("snapshot captured %d format(s): %s",
              len(formats), [_format_name(f) for f in formats])
    return ClipboardSnapshot(formats=formats)


def set_clipboard_text(text: str) -> None:
    """Replace the clipboard with CF_UNICODETEXT `text`.

    Raises ClipboardLockedError if the clipboard can't be opened. Also used
    by the History viewer's Copy action (user-initiated overwrite — that's
    the point there).
    """
    _open_clipboard_with_retry()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def restore_clipboard(snap: ClipboardSnapshot) -> bool:
    """Write every captured format back (§8.5). True only on a full restore.

    Per-format write failures are logged and the rest still restore — the
    user gets back as much of their clipboard as Windows allows. An empty
    snapshot restores to an empty clipboard.
    """
    try:
        _open_clipboard_with_retry()
    except ClipboardLockedError as e:
        log.warning("restore failed — clipboard locked: %s", e)
        return False
    all_ok = True
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in snap.formats.items():
            try:
                if fmt == win32con.CF_HDROP:
                    win32clipboard.SetClipboardData(
                        fmt, _pack_dropfiles(data))  # type: ignore[arg-type]
                else:
                    # str for CF_UNICODETEXT, raw bytes for everything else —
                    # pywin32 copies either into a fresh HGLOBAL for us.
                    win32clipboard.SetClipboardData(fmt, data)
            except (pywintypes.error, TypeError) as e:
                log.warning("restore: format %s failed to write back: %s",
                            _format_name(fmt), e)
                all_ok = False
    except pywintypes.error as e:
        # EmptyClipboard itself failed — nothing was restored.
        log.warning("restore failed emptying clipboard: %s", e)
        return False
    finally:
        win32clipboard.CloseClipboard()
    return all_ok


# ---------------------------------------------------------------------- #
# Foreground precheck (v2 auto-paste — stale-hwnd semantics)              #
# ---------------------------------------------------------------------- #

def foreground_verdict(target_hwnd: int) -> str:
    """Classify the CURRENT foreground window against the hwnd captured at
    hotkey-release time (v2 plan U2 — the auto-paste stale-window check).

    Returns one of four stable strings the caller maps to paste/skip:

        "same"   GetForegroundWindow() == target_hwnd     -> paste
        "none"   no foreground window at all (transient:
                 toast/UAC/desktop gap)                    -> paste
                 (paste_into's restore_focus handles it)
        "own"    the foreground hwnd belongs to THIS
                 process (our pill/toast grabbed focus)    -> paste
        "other"  a different app's window is foreground
                 (the user Alt-Tabbed away while the
                 pipeline was busy)                        -> DO NOT paste

    On "other" the caller must never call SetForegroundWindow — yanking the
    user back to a window they deliberately left is the exact UX crime v2
    exists to avoid. Worker-thread-only, like everything else here.
    """
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return "none"
    if fg == target_hwnd:
        return "same"
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(fg)
    except pywintypes.error as e:
        # Foreground window died between the two calls — treat like "none"
        # (transient); paste_into's own IsWindow/focus checks still guard.
        log.debug("foreground_verdict: pid lookup for 0x%X raised: %s", fg, e)
        return "none"
    return "own" if pid == os.getpid() else "other"


# ---------------------------------------------------------------------- #
# Focus return                                                            #
# ---------------------------------------------------------------------- #

def _try_set_foreground(hwnd: int) -> bool:
    """One SetForegroundWindow attempt, verified via GetForegroundWindow."""
    try:
        win32gui.SetForegroundWindow(hwnd)
    except pywintypes.error as e:
        log.debug("SetForegroundWindow(0x%X) raised: %s", hwnd, e)
        return False
    time.sleep(_FOCUS_SETTLE_SEC)
    return win32gui.GetForegroundWindow() == hwnd


def restore_focus(hwnd: int) -> bool:
    """Return foreground focus to `hwnd`. False if the window died or
    Windows' foreground lock could not be satisfied.

    Order (plan §6 Chunk 5): validate IsWindow -> already-foreground
    short-circuit -> SetForegroundWindow -> on failure, press+release
    VK_MENU (Alt) via pynput to credit this process with input, re-validate
    the hwnd, retry once.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    if _try_set_foreground(hwnd):
        return True

    # Foreground-lock rule fallback: a synthesized Alt tap makes Windows
    # consider this process recently-input-receiving, unlocking one
    # SetForegroundWindow. (The Alt keyup alone activates no menus in the
    # target because it isn't the foreground window yet; our own hotkey
    # filter ignores everything but VK_RCONTROL, so the tap is inert.)
    kb = keyboard.Controller()
    kb.press(keyboard.Key.alt)
    kb.release(keyboard.Key.alt)
    time.sleep(_FOCUS_SETTLE_SEC)

    if not win32gui.IsWindow(hwnd):  # window died between attempts
        return False
    return _try_set_foreground(hwnd)


# ---------------------------------------------------------------------- #
# The full injection flow                                                 #
# ---------------------------------------------------------------------- #

def paste_into(hwnd: int, text: str, restore_delay_ms: int) -> InjectResult:
    """snapshot -> restore_focus -> set clipboard -> Ctrl+V -> delay -> restore.

    §8.5 contract: the user's clipboard ALWAYS comes back. On the abort
    paths (window_gone / focus) the payload was never written, so the
    clipboard is left exactly as found — no consolation clipboard. The
    transcript's safety net is History (main.py records it with the
    matching outcome and toasts where to find it).

    Result decoding (reasons are stable strings main.py maps to toasts):
      ok=False, reason="window_gone"    target hwnd no longer exists;
                                         clipboard untouched — transcript
                                         is in History
      ok=False, reason="focus"           window alive but focus return
                                         failed; clipboard untouched —
                                         transcript is in History
      ok=False, reason="clipboard"       clipboard locked/failed writing the
                                         paste payload — nothing pasted;
                                         best-effort restore attempted
      ok=True,  reason="snapshot"        pasted, but the pre-paste snapshot
                                         failed (locked) — previous
                                         clipboard content is gone
      ok=True,  reason="restore_failed"  pasted; one or more captured
                                         formats failed to write back
                                         (partial restore — see log)
      ok=True,  reason=None              pasted and previous clipboard
                                         fully restored
    """
    # 1. Snapshot what the user had copied (may fail: proceed without).
    snap: ClipboardSnapshot | None = None
    try:
        snap = snapshot_clipboard()
    except ClipboardLockedError as e:
        log.warning("clipboard snapshot failed, proceeding without: %s", e)

    # 2. Focus return — abort cleanly if the target is gone or unfocusable.
    #    Nothing has been written yet, so the clipboard is exactly as found
    #    (§8.5: no consolation clipboard — History is the safety net).
    #    (The review popup is already hidden by the time we run — see
    #    main.py's accept-path ordering note.)
    if not hwnd or not win32gui.IsWindow(hwnd):
        log.info("inject aborted: target hwnd 0x%X is gone (clipboard untouched)",
                 hwnd or 0)
        return InjectResult(ok=False, clipboard_restored=True, reason="window_gone")

    if not restore_focus(hwnd):
        log.info("inject aborted: could not return focus to hwnd 0x%X "
                 "(clipboard untouched)", hwnd)
        return InjectResult(ok=False, clipboard_restored=True, reason="focus")

    # 3. Payload onto the clipboard.
    try:
        set_clipboard_text(text)
    except (ClipboardLockedError, pywintypes.error) as e:
        log.warning("inject aborted: clipboard write for payload failed: %s", e)
        # Best-effort: if the failure landed after EmptyClipboard, put the
        # user's content back. (If the open itself failed this is a no-op
        # rewrite of what's already there — harmless.)
        restored = restore_clipboard(snap) if snap is not None else False
        return InjectResult(ok=False, clipboard_restored=restored,
                            reason="clipboard")

    # 4. Ctrl+V. pynput sends a generic VK_CONTROL (0x11); Windows resolves
    #    that non-extended scan code to VK_LCONTROL (0xA2) at the low-level
    #    hook — confirmed via a live hook capture — which is distinct from
    #    the VK_RCONTROL (0xA3) hotkey, so our own filter passes it through.
    #    NOTE: this safety property is tied to the hotkey being ctrl_r
    #    specifically. If config.json's hotkey is ever changed to ctrl_l,
    #    this synthesized paste would self-match and re-trigger on_press/
    #    on_release (the filter doesn't check LLKHF_INJECTED). Fine today;
    #    revisit if the hotkey ever moves onto a Ctrl variant.
    kb = keyboard.Controller()
    with kb.pressed(keyboard.Key.ctrl):
        kb.press("v")
        kb.release("v")

    # 5. Let the target app read the clipboard before we touch it again.
    time.sleep(restore_delay_ms / 1000.0)

    # 6. Restore whatever the user had (§8.5: always, best-effort per format).
    if snap is None:
        return InjectResult(ok=True, clipboard_restored=False, reason="snapshot")
    if restore_clipboard(snap):
        log.info("pasted %d chars into hwnd 0x%X, clipboard restored "
                 "(%d format(s))", len(text), hwnd, len(snap.formats))
        return InjectResult(ok=True, clipboard_restored=True, reason=None)
    return InjectResult(ok=True, clipboard_restored=False, reason="restore_failed")
