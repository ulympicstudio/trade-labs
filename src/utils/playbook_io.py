"""Playbook I/O — load / query saved playbook symbols.

The OFF_HOURS pipeline writes ``data/playbook_latest.json`` every publish
cycle.  This module provides a single helper to read that file and return
the list of symbols that made it into the playbook (for PREMARKET priority
polling, opening-plan scoring, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.monitoring.logger import get_logger

log = get_logger("playbook_io")

_DEFAULT_PATH = "data/playbook_latest.json"


def load_playbook_symbols(path: str = _DEFAULT_PATH) -> List[str]:
    """Return the sorted unique list of symbols from the latest playbook.

    Parameters
    ----------
    path:
        Relative or absolute path to the playbook JSON file.  Defaults to
        ``data/playbook_latest.json``.

    Returns
    -------
    list[str]
        Alphabetically sorted unique symbols found in the ``"drafts"``
        array.  Returns an empty list if the file is missing or malformed.
    """
    p = Path(path)
    if not p.exists():
        log.info("Playbook file not found: %s — returning empty list", p)
        return []

    try:
        data = json.loads(p.read_text())
        drafts: List[Dict[str, Any]] = data.get("drafts", [])
        symbols = sorted({d["symbol"] for d in drafts if "symbol" in d})
        log.info(
            "Loaded %d playbook symbols from %s  (updated=%s)",
            len(symbols),
            p,
            data.get("updated", "?"),
        )
        return symbols
    except Exception:
        log.exception("Failed to read playbook file %s", p)
        return []


def load_playbook_drafts(path: str = _DEFAULT_PATH) -> List[Dict[str, Any]]:
    """Return the full drafts list from the latest playbook JSON.

    Each element is a dict with keys like ``symbol``, ``entry``, ``stop``,
    ``total_score``, ``quality``, etc.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("drafts", [])
    except Exception:
        log.exception("Failed to read playbook drafts from %s", p)
        return []


def get_playbook_entry_price(
    path: str = _DEFAULT_PATH, symbol: str = "",
) -> Optional[float]:
    """Return the suggested entry price for *symbol* from the playbook, or None."""
    for d in load_playbook_drafts(path):
        if d.get("symbol") == symbol:
            return d.get("entry")
    return None
