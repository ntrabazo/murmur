"""Thread-safe pipeline state machine for flow-clone (Chunk 4, v2 flow).

v2 (auto-paste): `IDLE → RECORDING → TRANSCRIBING → CLEANING → INJECTING
→ IDLE`, guarded by a threading.Lock. (The v1 REVIEW state died with the
review popup — dictation now flows straight into injection.) A hotkey press
in any non-IDLE state is ignored (+ error beep in the caller) — this is the
re-entrancy guard that prevents two pipelines racing over one clipboard.

try_transition() is compare-and-swap: the check and the assignment happen
under one lock acquisition, so two threads (pynput hook thread + worker
thread) can never both win the same transition. A False return means
"ignore this event", never an error.

force() exists for exactly one caller: the worker's top-of-loop exception
handler, which must reset to IDLE no matter what state the pipeline died in
(plan §6 Chunk 4 failure modes).
"""

from __future__ import annotations

import logging
import threading
from enum import Enum, auto

log = logging.getLogger(__name__)


class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    CLEANING = auto()
    INJECTING = auto()


class AppState:
    """Lock-guarded current-state holder with CAS transitions."""

    def __init__(self, initial: State = State.IDLE) -> None:
        self._lock = threading.Lock()
        self._state = initial

    @property
    def state(self) -> State:
        """Snapshot of the current state (may be stale the instant it returns —
        use try_transition() for any decision that must be race-free)."""
        with self._lock:
            return self._state

    def try_transition(self, from_: State | tuple[State, ...], to: State) -> bool:
        """Atomically move ``from_ -> to``.

        ``from_`` may be a single State or a tuple of acceptable source
        states. Returns True if the transition happened; False means the
        machine was in some other state and the caller must ignore the event
        (re-entrancy guard semantics — False is normal, not an error).
        """
        if isinstance(from_, State):
            from_ = (from_,)
        with self._lock:
            if self._state in from_:
                old, self._state = self._state, to
                log.debug("state %s -> %s", old.name, to.name)
                return True
            log.debug(
                "transition to %s refused (state is %s, wanted %s)",
                to.name, self._state.name, "/".join(s.name for s in from_),
            )
            return False

    def force(self, to: State) -> None:
        """Unconditional reset — ONLY for the worker's exception handler
        (state must return to IDLE no matter where the pipeline died)."""
        with self._lock:
            old, self._state = self._state, to
        log.warning("state FORCED %s -> %s", old.name, to.name)
