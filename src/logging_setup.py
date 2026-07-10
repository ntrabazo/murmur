"""Rotating-file logging for flow-clone (Chunk 7, plan §6).

Replaces main.py's Chunk-4 ``_setup_logging()`` (plain FileHandler +
StreamHandler via basicConfig) with the spec'd rotating handler:

    RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3,
                        encoding="utf-8")
    format: "%(asctime)s %(levelname)s %(name)s: %(message)s"
    root logger at INFO

pythonw caveat (load-bearing once flow-clone.cmd exists): under pythonw.exe
there is NO console — ``sys.stderr`` is ``None``. ``logging.StreamHandler()``
defaults to ``sys.stderr`` *at emit time via its ``stream`` attribute set at
construction*, so constructing one with a ``None`` stream produces a handler
that raises/black-holes on every record. The console handler is therefore
only attached when a real stderr exists (console launches: ``python
src\\main.py``); under pythonw the rotating file is the only sink, which is
exactly what we want.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_path: Path = Path("logs/flowclone.log")) -> logging.Logger:
    """Configure the root logger per plan §6 Chunk 7 and return it.

    - Rotating file: 1 MB x 3 backups, UTF-8 (flowclone.log, .1, .2, .3).
    - Console StreamHandler added ONLY when sys.stderr is a real stream
      (absent under pythonw — see module docstring).
    - Replaces any existing root handlers (idempotent: safe to call after a
      stray basicConfig, and calling twice never double-logs).
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace, don't append — repeated configuration must not duplicate lines.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.addHandler(file_handler)

    if sys.stderr is not None:  # real console (python.exe) — mirror to it
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    return root
