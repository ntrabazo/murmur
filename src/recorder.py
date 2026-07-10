"""Persistent-stream audio recorder for flow-clone (Chunk 1).

Load-bearing design (plan §3.5): the sounddevice InputStream is opened ONCE at
app startup via open_stream() (and again on tray Resume) and left running for
the life of the app. It is NEVER opened or closed on the hotkey path — the
pynput hook thread has a hard sub-millisecond budget before Windows silently
unhooks it, and PortAudio/WASAPI stream-open is not a bounded-time operation.

arm() / disarm_and_get() are therefore pure in-memory operations (an Event
flip, a list clear, an atomic list swap) and are safe to call directly from
the pynput callback thread.

Trade-off, stated in the plan: the Windows mic-in-use indicator stays lit the
whole time the stream is open. Tray Pause (Chunk 7) wires close_stream() to
make that opt-out.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


class Recorder:
    """Continuously-running input stream with an armable in-memory buffer.

    The PortAudio callback fires ~every 30ms on its own thread and appends to
    ``self._frames`` only while ``self._armed`` is set. All buffer handoff is
    done with an atomic swap so the callback thread and the caller never
    mutate the same list concurrently.
    """

    def __init__(self, sample_rate: int = 16000, max_sec: int = 90) -> None:
        self.sample_rate = sample_rate
        self.max_sec = max_sec
        self._armed = threading.Event()
        self._frames: list[np.ndarray] = []
        self._armed_samples = 0  # samples captured since last arm(); max_sec guard
        self._stream: sd.InputStream | None = None
        self.overflowed = False  # set by the callback when max_sec auto-disarm fires
        # v2 U5: rolling RMS level in [0, 1] for the live pill meter. Written
        # by the PortAudio callback, read (unlocked) by the tk main thread —
        # a single float assignment is GIL-atomic, same pattern as .overflowed.
        self.level = 0.0

    # ------------------------------------------------------------------ #
    # Stream lifecycle — called ONCE at startup / tray Pause-Resume only. #
    # NEVER call these from the hotkey path (see module docstring).       #
    # ------------------------------------------------------------------ #

    def open_stream(self) -> None:
        """Open and start the persistent InputStream.

        Raises whatever sounddevice raises if there is no default input
        device or the device fails to open — the caller (startup / tray
        Resume) treats that as fatal-toast-and-exit per the plan.
        """
        if self._stream is not None:
            return  # already open — idempotent
        # Fails loudly here (not later, mid-dictation) if no input device.
        sd.query_devices(kind="input")
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            callback=self._cb,
        )
        try:
            stream.start()
        except Exception:
            # A constructed-but-unstartable stream must not be retained —
            # the idempotency guard above would make the next Resume
            # falsely report success (review-caught corner case).
            stream.close()
            raise
        self._stream = stream
        log.info("audio stream opened (%d Hz, mono, float32)", self.sample_rate)

    def close_stream(self) -> None:
        """Stop and close the stream (tray Pause / app shutdown)."""
        stream, self._stream = self._stream, None
        self._armed.clear()
        if stream is not None:
            stream.stop()
            stream.close()
            log.info("audio stream closed")

    # ------------------------------------------------------------- #
    # Hot path — pure in-memory, sub-millisecond, no PortAudio calls #
    # ------------------------------------------------------------- #

    def arm(self) -> None:
        """Begin capturing. Pure in-memory: clear buffer, set flag."""
        # Clear BEFORE setting the flag so a stray chunk appended by the
        # callback during a previous disarm race can never leak into this
        # recording.
        self._frames.clear()
        self._armed_samples = 0
        self.overflowed = False
        self._armed.set()

    def disarm_and_get(self) -> np.ndarray:
        """Stop capturing and return the audio captured since arm().

        Atomic swap: the callback thread may hold a reference to the old
        list, but it can never touch the new one mid-read, so there is no
        read/append race.

        REQUIRED guard: returns an empty float32 array when nothing was
        captured — ``np.concatenate([])`` raises ValueError, and the empty
        path is genuinely reachable (a release firing before the first
        ~30ms callback lands, or any sub-debounce tap; the debounce in the
        caller discards the *result*, it never skips calling this).
        """
        self._armed.clear()
        frames, self._frames = self._frames, []  # atomic swap
        if not frames:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(frames).reshape(-1)

    # ------------------------------------------------ #
    # PortAudio callback — runs on the PortAudio thread #
    # ------------------------------------------------ #

    def _cb(self, indata, frames, time, status) -> None:
        if status:
            # Overflow/underflow etc. — log and keep going, never fatal.
            log.warning("audio callback status: %s", status)
        # v2 U5: update the live level every chunk, armed or not, so the pill
        # meter reacts the instant recording starts. EMA smoothing (~0.4s
        # rise/fall at 30ms chunks) keeps the bars from strobing; the ~6.3x
        # gain maps typical speech RMS (~0.02-0.15) toward a usable [0,1].
        rms = float(np.sqrt(np.mean(np.square(indata))))
        self.level = 0.7 * self.level + 0.3 * min(1.0, rms * 6.3)
        if not self._armed.is_set():
            return
        self._frames.append(indata.copy())
        self._armed_samples += len(indata)
        if self._armed_samples > self.max_sec * self.sample_rate:
            # Flush guard: auto-disarm to protect RAM and STT latency.
            # Captured frames are retained for the eventual disarm_and_get().
            self._armed.clear()
            self.overflowed = True
            log.warning(
                "recording exceeded max_sec=%ds -- auto-disarmed", self.max_sec
            )
