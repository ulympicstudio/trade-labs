"""
playbook_miner.py

Placeholder implementation so the rest of the system can run.

Why:
- src/signals/signal_validator.py imports names from this module.
- If the real playbook-mining logic isn't ready yet, we provide safe defaults.

Later:
- Replace these stubs with real pattern mining + scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PlaybookMatch:
    symbol: str
    score: float = 0.0
    reason: str = "playbook miner placeholder"


def mine_playbook(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return an empty playbook artifact (placeholder)."""
    return {"patterns": [], "meta": {"placeholder": True}}


def score_playbook_match(*args: Any, **kwargs: Any) -> float:
    """Return neutral score (placeholder)."""
    return 0.0


def get_playbook_matches(*args: Any, **kwargs: Any) -> List[PlaybookMatch]:
    """Return no matches (placeholder)."""
    return []


def enrich_with_playbook(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return empty enrichment (placeholder)."""
    return {"playbook": {"matches": [], "placeholder": True}}


def __getattr__(name: str):
    """
    Safety net:
    If signal_validator imports a name we didn't explicitly define,
    return a harmless stub function instead of crashing at import time.
    """
    def _stub(*args: Any, **kwargs: Any):
        return None
    return _stub