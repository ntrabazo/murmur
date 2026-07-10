"""Headless Chunk 7 verification: configure_logging() — spec'd format,
1MB x 3 rotation, idempotent reconfiguration (no duplicate lines).

Root-logger hygiene: configure_logging replaces the root handlers, so every
test snapshots and restores them — the rest of the suite's logging must be
untouched by this file.

Windows detail: ``_preserved_root()`` must be the INNER context manager so it
exits (and closes the rotating handler, releasing the file lock) BEFORE
TemporaryDirectory tries to delete the log file.

Runnable two ways from the project root:
    .\\.venv\\Scripts\\python tests\\test_logging_setup.py     (plain asserts)
    .\\.venv\\Scripts\\python -m pytest tests\\test_logging_setup.py
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.logging_setup import configure_logging  # noqa: E402

#: "%(asctime)s %(levelname)s %(name)s: %(message)s" with default asctime.
_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO [\w.]+: .+$"
)


@contextlib.contextmanager
def _preserved_root():
    """Snapshot root handlers/level; afterwards close ours and restore."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        yield root
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()  # release the file so TemporaryDirectory can delete
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_format_matches_spec_and_level_info() -> None:
    with tempfile.TemporaryDirectory() as td, _preserved_root():
        log_path = Path(td) / "logs" / "flowclone.log"  # parent auto-created
        configure_logging(log_path)
        logging.getLogger("flowclone.test").info("format probe")
        logging.getLogger("flowclone.test").debug("must NOT appear (INFO root)")
        for h in logging.getLogger().handlers:
            h.flush()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, lines
        assert _LINE_RE.match(lines[0]), f"format mismatch: {lines[0]!r}"


def test_rotation_1mb_backup1() -> None:
    with tempfile.TemporaryDirectory() as td, _preserved_root():
        log_path = Path(td) / "flowclone.log"
        configure_logging(log_path)
        # Drop the console mirror for this test only — 1.2MB of payload
        # lines belongs in the file under test, not in pytest's captured
        # stderr. (The file handler is the thing being verified.)
        root = logging.getLogger()
        for h in root.handlers[:]:
            if not isinstance(h, logging.handlers.RotatingFileHandler):
                root.removeHandler(h)
        log = logging.getLogger("flowclone.rotate")
        payload = "x" * 1000
        for _ in range(1200):  # ~1.2MB of records > maxBytes=1_000_000
            log.info(payload)
        for h in logging.getLogger().handlers:
            h.flush()
        assert log_path.exists()
        backup = log_path.with_name(log_path.name + ".1")
        assert backup.exists(), "rotation never produced flowclone.log.1"
        assert backup.stat().st_size <= 1_000_000 + 2048, \
            "backup exceeds maxBytes by more than one record"
        assert log_path.stat().st_size < 1_000_000


def test_pythonw_no_console_handler() -> None:
    """Under pythonw sys.stderr is None — configure_logging must NOT build a
    StreamHandler around it (StreamHandler(None) would bind sys.stderr at
    construction time and write nowhere useful / raise on emit)."""
    with tempfile.TemporaryDirectory() as td, _preserved_root():
        saved_stderr = sys.stderr
        sys.stderr = None  # simulate pythonw
        try:
            configure_logging(Path(td) / "flowclone.log")
            handlers = logging.getLogger().handlers
            assert len(handlers) == 1, handlers
            assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
            # and logging still works, straight to the file
            logging.getLogger("flowclone.headless").info("no console, no crash")
            handlers[0].flush()
            assert "no console, no crash" in (
                (Path(td) / "flowclone.log").read_text(encoding="utf-8")
            )
        finally:
            sys.stderr = saved_stderr


def test_console_handler_present_with_real_stderr() -> None:
    with tempfile.TemporaryDirectory() as td, _preserved_root():
        configure_logging(Path(td) / "flowclone.log")
        kinds = [type(h) for h in logging.getLogger().handlers]
        assert logging.handlers.RotatingFileHandler in kinds
        assert logging.StreamHandler in kinds  # console mirror when one exists


def test_reconfigure_does_not_duplicate_lines() -> None:
    with tempfile.TemporaryDirectory() as td, _preserved_root():
        log_path = Path(td) / "flowclone.log"
        configure_logging(log_path)
        configure_logging(log_path)  # second call must REPLACE, not append
        logging.getLogger("flowclone.dup").info("once only")
        for h in logging.getLogger().handlers:
            h.flush()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert sum("once only" in ln for ln in lines) == 1, lines


# ---------------------------------------------------------------------- #
# Plain-python runner                                                     #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
