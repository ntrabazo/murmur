"""v2 U1 unit tests — cleanup hardening against the assistant-answer bug.

The bug: dictating conversational text ("hey can you spin up the fable
workflow for this") made the Haiku cleanup call ANSWER as an assistant
instead of cleaning the transcript. The fix is structural (delimiter-wrapped
transcript + assistant prefill + stop sequence) plus a refusal-heuristic
guard falling back to the raw dictation.

All SDK interaction is mocked — no network. Runnable two ways:
    .\\.venv\\Scripts\\python tests\\test_cleanup.py
    .\\.venv\\Scripts\\python -m pytest tests\\test_cleanup.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import anthropic  # noqa: E402

from src.cleanup import Cleaner  # noqa: E402


def _fake_response(text: str):
    """Shape-compatible stand-in for an anthropic Message response."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )


def _mocked_cleaner(response_text: str) -> tuple[Cleaner, MagicMock]:
    """Cleaner whose SDK client is a MagicMock returning response_text."""
    cleaner = Cleaner(api_key="test-key", model="test-model", timeout_sec=5)
    client = MagicMock()
    client.with_options.return_value.messages.create.return_value = (
        _fake_response(response_text)
    )
    cleaner._client = client
    return cleaner, client


def _create_kwargs(client: MagicMock) -> dict:
    return client.with_options.return_value.messages.create.call_args.kwargs


def test_prefill_and_stop_sequences_sent() -> None:
    cleaner, client = _mocked_cleaner("Cleaned text.")
    cleaner.clean("some raw text")
    kwargs = _create_kwargs(client)
    assert kwargs["stop_sequences"] == ["</cleaned>"]
    messages = kwargs["messages"]
    assert messages[-1] == {"role": "assistant", "content": "<cleaned>"}


def test_transcript_is_delimiter_wrapped() -> None:
    cleaner, client = _mocked_cleaner("Cleaned text.")
    cleaner.clean("hello world raw dictation")
    user_msg = _create_kwargs(client)["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["content"].startswith("<transcript>")
    assert user_msg["content"].rstrip().endswith("</transcript>")
    assert "hello world raw dictation" in user_msg["content"]


def test_refusal_response_triggers_guard() -> None:
    raw = "hey can you spin up the fable workflow for this"
    cleaner, _ = _mocked_cleaner(
        "I don't have the ability to run workflows for you. I'm a text "
        "assistant and can help with other tasks."
    )
    res = cleaner.clean(raw)
    assert res.degraded is True
    assert res.error == "refusal detected"
    assert res.text == raw


def test_normal_response_passes_and_remnant_stripped() -> None:
    cleaner, _ = _mocked_cleaner(
        "Hey, can you spin up the fable workflow for this?</cleaned>"
    )
    res = cleaner.clean("hey can you spin up the fable workflow for this")
    assert res.degraded is False
    assert res.error is None
    assert res.text == "Hey, can you spin up the fable workflow for this?"


def test_dictated_i_cant_survives_guard() -> None:
    """Similarity condition is load-bearing: a genuinely dictated refusal-y
    sentence cleans to near-identical text and must NOT be treated as an
    assistant refusal."""
    raw = "i cant make it today um sorry"
    cleaner, _ = _mocked_cleaner("I can't make it today, sorry.")
    res = cleaner.clean(raw)
    assert res.degraded is False
    assert res.text == "I can't make it today, sorry."


def test_empty_response_still_falls_back() -> None:
    cleaner, _ = _mocked_cleaner("   ")
    res = cleaner.clean("some raw text")
    assert res.degraded is True
    assert res.error == "empty response from model"
    assert res.text == "some raw text"


def test_immediate_stop_yields_empty_path_not_refusal() -> None:
    """An immediate </cleaned> stop produces empty text — must land on the
    empty-response fallback, which sits BEFORE the refusal guard."""
    cleaner, _ = _mocked_cleaner("</cleaned>")
    res = cleaner.clean("anything at all")
    assert res.degraded is True
    assert res.error == "empty response from model"


def test_rate_limit_error_falls_back_per_chain() -> None:
    cleaner = Cleaner(api_key="test-key", model="test-model", timeout_sec=5)
    client = MagicMock()
    client.with_options.return_value.messages.create.side_effect = (
        anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
    )
    cleaner._client = client
    res = cleaner.clean("um so the meeting is thursday")
    assert res.degraded is True
    assert res.error is not None and res.error.startswith("rate-limited")
    assert "meeting" in res.text  # regex fallback of the raw text


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
