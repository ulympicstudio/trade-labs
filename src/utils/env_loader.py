"""
.env validation utility.

Validates that every non-blank, non-comment line matches ``KEY=value`` format.
Logs a warning for any malformed line so bare API keys never cause
``command not found`` errors when the file is ``source``-d.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple

from src.monitoring.logger import get_logger

log = get_logger("env_loader")

_VALID_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")  # KEY=...


def validate_env(env_path: str | Path | None = None) -> List[Tuple[int, str]]:
    """Validate ``.env`` file format.

    Parameters
    ----------
    env_path:
        Path to the ``.env`` file.  Defaults to ``<project_root>/.env``.

    Returns
    -------
    list[tuple[int, str]]
        List of ``(line_number, line_text)`` for invalid lines.
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
    env_path = Path(env_path)

    if not env_path.exists():
        log.warning(".env file not found at %s", env_path)
        return []

    bad_lines: List[Tuple[int, str]] = []
    with open(env_path) as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            # Blank lines and comments are fine
            if not line or line.startswith("#"):
                continue
            if not _VALID_LINE_RE.match(line):
                bad_lines.append((lineno, line))
                log.warning(
                    "Invalid .env line %d ignored: %s",
                    lineno,
                    line[:60] + ("…" if len(line) > 60 else ""),
                )

    if not bad_lines:
        log.info(".env validation passed (%s)", env_path)
    else:
        log.warning(
            ".env validation: %d invalid line(s) found — "
            "ensure every line is KEY=value, blank, or starts with #",
            len(bad_lines),
        )
    return bad_lines
