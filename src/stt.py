"""faster-whisper STT wrapper for flow-clone (Chunk 2).

Design notes (plan §6 Chunk 2):
- The model loads ONCE at app startup on the worker thread via load(),
  never per-dictation. First run downloads ~460MB into the project-local
  data/models/ cache (download_root) instead of the default HF cache in the
  user profile — keeps the tool self-contained and the download visible in
  .gitignore.
- transcribe() takes the float32 16kHz mono numpy array straight from
  Recorder.disarm_and_get() — no temp WAV in the real pipeline.
- vad_filter=True trims leading/trailing silence, which meaningfully cuts
  latency on hold-to-talk audio.
- initial_prompt is the hook Chunk 6's dictionary uses to bias the decoder
  toward known proper nouns.

Failure modes (plan §6):
- First-run download fails (offline) / model load failure → load() lets the
  exception propagate; the caller (main.py startup) treats it as fatal
  toast + exit. The app is useless without STT.
- Empty transcription (silence / empty buffer) → SttResult(text="") without
  crashing; the pipeline toasts "heard nothing" and returns to IDLE.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # faster-whisper expects 16kHz when fed a raw ndarray


@dataclass
class SttResult:
    text: str
    audio_sec: float
    latency_sec: float


class SttEngine:
    """Lifecycle wrapper around faster_whisper.WhisperModel (CPU, int8)."""

    def __init__(
        self,
        model_size: str,
        compute_type: str,
        cpu_threads: int,
        cache_dir: Path,
    ) -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.cache_dir = Path(cache_dir)
        self._model: WhisperModel | None = None

    def load(self) -> float:
        """Load the model (downloading into cache_dir on first run).

        Returns load time in seconds. Idempotent: a second call with the
        model already loaded returns ~0 without reloading.

        Raises whatever faster-whisper/huggingface-hub raises on download or
        load failure — caller treats that as fatal (plan §6 failure modes).
        """
        if self._model is not None:
            return 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        self._model = WhisperModel(
            self.model_size,
            device="cpu",
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            download_root=str(self.cache_dir),
        )
        load_sec = time.perf_counter() - t0
        log.info(
            "model %s loaded in %.2fs (compute_type=%s, cpu_threads=%d, cache=%s)",
            self.model_size, load_sec, self.compute_type, self.cpu_threads,
            self.cache_dir,
        )
        return load_sec

    def transcribe(
        self, audio: np.ndarray, initial_prompt: str | None = None
    ) -> SttResult:
        """Transcribe a float32 16kHz mono ndarray.

        Returns SttResult(text="") for empty/silent audio instead of
        crashing (plan §6 failure mode — pipeline toasts "heard nothing").
        """
        if self._model is None:
            raise RuntimeError("SttEngine.transcribe() called before load()")

        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        audio_sec = audio.size / SAMPLE_RATE

        # Empty-buffer guard: Recorder.disarm_and_get() can legitimately hand
        # back a zero-length array (sub-debounce tap). Don't feed that to the
        # model at all.
        if audio.size == 0:
            return SttResult(text="", audio_sec=0.0, latency_sec=0.0)

        t0 = time.perf_counter()
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=5,
            vad_filter=True,
            initial_prompt=initial_prompt,
        )
        # segments is a lazy generator — joining it is what actually runs
        # the transcription, so the join stays inside the latency window.
        text = " ".join(seg.text.strip() for seg in segments).strip()
        latency_sec = time.perf_counter() - t0

        log.info(
            "transcribed %.2fs audio in %.2fs (%d chars)",
            audio_sec, latency_sec, len(text),
        )
        return SttResult(text=text, audio_sec=audio_sec, latency_sec=latency_sec)
