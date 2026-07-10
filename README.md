# Murmur

**Local voice dictation for Windows that learns from its own mistakes.** Hold a hotkey,
speak, release. The text lands in whatever app you were dictating into. Transcription runs
entirely on-device, and every time you fix a mishearing, Murmur learns the correction and
stops making it.

The app is the vehicle. The engineering is the point: a real-time audio pipeline, a
self-correcting learning loop, and the unglamorous systems work that makes a desktop tool
trustworthy, like clipboard-preserving text injection and crash-safe file writes.

## How it works

```
hold Right Alt ──► record ──► on-device STT ──► LLM cleanup ──► auto-paste into
                    (mic)    (faster-whisper)  (Claude Haiku,    the app you were
                                   ▲           offline fallback)  dictating into
                                   │                  ▲
                                   └── learned ───────┘
                                       dictionary
```

- **On-device transcription.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  runs locally; audio never leaves the machine.
- **Cleanup pass.** The raw transcript goes through Claude Haiku for punctuation, casing,
  and filler removal. The transcript is passed as delimiter-wrapped data with a prefilled
  response and stop sequence, so dictating a question gets it cleaned up, not answered.
  A refusal guard falls back to the raw transcript if the model does anything else. No
  connection? A regex fallback degrades gracefully. Dictation never dies because Wi-Fi did.
- **Focus-aware injection.** Before pasting, Murmur verifies the window you dictated into
  still has focus. If you switched apps mid-pipeline, it skips the paste instead of yanking
  focus or typing into the wrong window. The transcript waits in History.
- **Clipboard preservation.** Injection is paste-based with a full binary clipboard snapshot
  and restore. Whatever you had copied before dictating (text, files, rich content) is still
  there afterward.

## The learning loop

The standout feature. Murmur watches what you do with its output, through two paths:

- after a paste, a background watcher reads the target field for a short window and diffs
  any in-place fixes you make against what was pasted;
- any transcript in the app window can be corrected by hand ("Edit & teach").

Both paths feed a diff learner that decides whether you *corrected a mishearing* or just
*rewrote the sentence*:

- a **rewrite guard** rejects wholesale edits (repeat-aware: fixing the same mishearing in
  five places is one strong signal, not a rewrite);
- a **similarity filter** requires the correction to sound like the mistake, so content
  edits don't get learned;
- everything else is **learned, on purpose**. Names, jargon, accent mishearings, even pure
  capitalization fixes — if you corrected it, Murmur respects it. The user decides what
  belongs in their dictionary, not a heuristic;
- everything learned is **visible**: a toast announces each learned pair, and every entry
  can be disabled or deleted from the Dictionary tab — that, plus an occasional audit, is
  the junk control.

Learned pairs land in a dictionary that feeds the *next* dictation twice: as a glossary in
Whisper's decoder prompt, and as explicit misheard→correct pairs in the cleanup model's
context. Accuracy on your own vocabulary compounds over time.

**[Try the learning loop in your browser](https://nicolastrabazo.com/)**: the Murmur card
on my portfolio runs the app's learning loop, ported to JavaScript.

## Living on the desktop

Murmur is a tray app with a real app window, built to be a daily driver:

- **Transcripts tab**: recent dictations with outcomes; copy any of them, or Edit & teach.
- **Dictionary tab**: every learned pair, with enable/disable/delete.
- **Pause from the tray** closes the microphone stream entirely (the Windows mic-in-use
  indicator goes off) and reopens it on resume.
- **Single instance.** Launching Murmur while it's already running just surfaces the running
  copy's window. Named-mutex exclusion, so there are no stale lock files to clean up.
- **Startup preflight.** On launch it checks for a second instance, an API key, a working
  input device, and the model cache, and reports the first failure in a dialog instead of
  dying silently in the background.

## Quickstart

Windows, Python 3.12:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # optional; without it cleanup uses the regex fallback
.venv\Scripts\python src\main.py
```

First run downloads the Whisper model (~460 MB) into `data/models/`. Then: focus any text
field, hold **Right Alt**, speak, release. Changed your mind mid-sentence? **Esc** cancels
the recording or the in-flight dictation — nothing pastes, and Esc only gets swallowed when
it actually cancelled something.

All tunables live in [`config.json`](config.json): hotkey, model size, injection mode,
learner thresholds.

## Engineering notes

- **Single-writer concurrency.** All dictionary and history mutation happens on one worker
  thread by construction, and the UI only ever renders snapshot copies, so nothing ever
  races on the data files.
- **Durability.** Atomic writes (`.tmp` + `os.replace`), corrupt-file quarantine instead of
  crashes, and mtime-based reload so hand-edits to the dictionary apply without a restart.
- **Honest failure modes.** Every injection failure maps to a specific toast. A failed
  microphone resume says so and stays paused instead of pretending it worked.
- **Logging for a windowless app.** Under `pythonw` there is no console, so a rotating file
  log is the only sink; a console mirror is attached only when a console actually exists.
- **Tested.** The learner, dictionary, history, state machine, startup checks, and
  single-instance logic are covered by a pytest suite (`pytest tests/`), including a real
  second-process exclusion test.

## Deliberate trade-offs

- The Right Alt hotkey is suppressed system-wide while Murmur runs, so AltGr characters and
  right-Alt combos don't type. Left Alt is untouched, and the binding is a one-line change
  in `config.json`.
- The microphone stream stays open while the app runs, so the Windows mic-in-use indicator
  is lit whenever Murmur is up. Tray Pause is the opt-out; it closes the stream for real.

## Privacy

Audio is processed in memory on-device. Transcript history and the learned dictionary are
local JSON files you can open, edit, or delete. The only network call is the optional
cleanup request, which sends the transcribed *text* (never audio) to the Anthropic API.

## License

MIT. See [LICENSE](LICENSE).
