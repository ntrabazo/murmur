"""Single-instance guard for flow-clone (Chunk 7, plan §6).

A named Windows mutex — the canonical single-instance mechanism. The
"Global\\" prefix puts it in the global kernel namespace so the check works
across sessions (fast user switching, RDP), not just this login session.

The mutex handle is deliberately kept alive at module level for the whole
process lifetime: the mutex exists as long as at least one open handle to it
exists, and is destroyed automatically when the owning process exits (clean
exit, crash, or kill — the kernel cleans up either way, so there is no stale
lock file to hand-clean, which is why this beats a PID file).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import win32api
import win32event
import winerror

log = logging.getLogger(__name__)

MUTEX_NAME = "Global\\FlowCloneSingleInstance"

#: A named auto-reset event used for "bring the running instance's window up"
#: IPC: a second launch (e.g. from Windows Search) can't start a real second
#: app (the mutex forbids it), so instead it SetEvents this and exits, and the
#: already-running primary opens its app window. Auto-reset so it re-arms
#: itself after each signal without manual reset.
SHOW_EVENT_NAME = "Global\\FlowCloneShowWindow"

#: Kept for the life of the process (see module docstring). GC'd at exit,
#: which closes the handle and (if we were the last holder) frees the name.
_mutex_handle = None
_show_event_handle = None


def acquire_single_instance_lock() -> bool:
    """Try to become THE flow-clone instance.

    Returns True if we are the first instance, False if another process
    already holds the named mutex. The handle is retained module-level
    either way (per plan §6: keep it open for the process lifetime; on the
    already-exists path the process is about to exit anyway).
    """
    global _mutex_handle
    _mutex_handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    # CreateMutex succeeds even when the mutex already exists — the signal
    # that another instance owns it is GetLastError() == ERROR_ALREADY_EXISTS,
    # which must be read immediately after the call.
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log.warning("single-instance mutex already held — another flow-clone is running")
        return False
    log.info("single-instance mutex acquired (%s)", MUTEX_NAME)
    return True


def signal_show_window() -> bool:
    """Tell the already-running instance to bring up its app window.

    Called by a SECOND launch after it finds the mutex held: open (or create)
    the named event and SetEvent, which the primary's listener thread is
    waiting on. Returns True on success. Best-effort — never raises into the
    caller (a failed signal just means the second launch exits without opening
    the window, no worse than the old "already running" dialog).
    """
    try:
        handle = win32event.CreateEvent(None, False, False, SHOW_EVENT_NAME)
        win32event.SetEvent(handle)
        win32api.CloseHandle(handle)
        log.info("signalled the running instance to show its window")
        return True
    except Exception:
        log.exception("could not signal show-window to the running instance")
        return False


def start_show_window_listener(on_show: Callable[[], None]) -> None:
    """Primary instance: wait for show-window signals and invoke on_show.

    Spawns a daemon thread that blocks on the named event; each time a second
    launch signals it, on_show() runs (main.py passes a closure that puts
    ("open_app",) on the ui queue — thread-safe — so the actual window show
    happens on the tk main thread via the poll loop). Auto-reset event, so
    the thread loops forever re-arming. Daemon: dies at process exit.
    """
    global _show_event_handle
    _show_event_handle = win32event.CreateEvent(None, False, False, SHOW_EVENT_NAME)

    def _loop() -> None:
        while True:
            try:
                win32event.WaitForSingleObject(_show_event_handle, win32event.INFINITE)
                on_show()
            except Exception:
                log.exception("show-window listener error (continuing)")

    threading.Thread(target=_loop, name="show-listener", daemon=True).start()
    log.info("show-window listener started (%s)", SHOW_EVENT_NAME)
