"""Config loading + Anthropic key check for flow-clone (Chunk 0).

- load_config(): reads config.json from the project root, filling any missing
  keys from DEFAULTS so later chunks can rely on every key existing.
- get_anthropic_key(): reads ANTHROPIC_API_KEY from C:\\Users\\<user>\\.env
  via python-dotenv; raises MissingKeyError with the exact fix if absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULTS: dict = {
    "hotkey": "f9",
    "suppress_hotkey": True,
    # "hold" = original single-gesture behavior (press, speak, release).
    # "dual" adds a double-tap latch on top: double-tap starts a hands-free
    # recording that a later tap stops, while a plain hold still works too.
    # Defaults to "hold" here (a fresh install falls back to the
    # longest-tested behavior); config.json carries the actual preference.
    "dictation_mode": "hold",
    "double_tap_sec": 0.35,
    "model_size": "small.en",
    "compute_type": "int8",
    "cpu_threads": 4,
    "model_cache_dir": "data/models",
    "sample_rate": 16000,
    "max_recording_sec": 90,
    "claude_model": "claude-haiku-4-5-20251001",
    "claude_timeout_sec": 10,
    "inject_mode": "paste",
    "paste_restore_delay_ms": 300,
    "max_dictionary_prompt_entries": 60,
    "min_correction_similarity": 0.45,
    "learned_entry_autoenable": True,
    "correction_watcher_enabled": True,
    "correction_watch_sec": 45,
    "correction_watch_poll_sec": 2.5,
}


class MissingKeyError(Exception):
    """Raised when ANTHROPIC_API_KEY cannot be found in the user's .env file."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load config.json and fill any missing keys from DEFAULTS.

    A missing or unreadable config file yields a pure-defaults dict rather
    than a crash; unknown extra keys in the file are preserved.
    """
    config = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            user_config = json.load(f)
        if isinstance(user_config, dict):
            config.update(user_config)
    except (OSError, json.JSONDecodeError):
        pass  # fall back to DEFAULTS entirely
    return config


def get_anthropic_key() -> str:
    """Return ANTHROPIC_API_KEY from C:\\Users\\<user>\\.env (via python-dotenv).

    Raises MissingKeyError with the exact line to add if the key is absent.
    """
    env_path = Path.home() / ".env"
    load_dotenv(env_path)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise MissingKeyError(
            f"ANTHROPIC_API_KEY not found in {env_path}. "
            'Add this line to that file and retry: ANTHROPIC_API_KEY=sk-ant-...'
        )
    return key
