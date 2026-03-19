"""
Persists kill switch state to disk so circuit breakers survive restarts.
State file: data/kill_switch_state.json
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(
    os.environ.get("TL_KS_STATE_PATH", "data/kill_switch_state.json")
)


def save_state(state: dict) -> None:
    """Atomically write *state* dict to disk with timestamp and session date."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "_saved_at": datetime.now(timezone.utc).isoformat(),
        "_session_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **state,
    }

    fd, tmp_path = tempfile.mkstemp(
        dir=str(STATE_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, str(STATE_PATH))
        log.debug("kill_switch state saved to %s", STATE_PATH)
    except Exception:
        log.exception("kill_switch state save failed")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def load_state() -> dict | None:
    """Load saved kill switch state.

    Returns *None* if the file is missing, corrupt, or from a previous
    trading day (stale state should not carry over).
    """
    if not STATE_PATH.exists():
        log.info("No kill switch state file at %s — starting fresh", STATE_PATH)
        return None

    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        log.exception("Corrupt kill switch state file at %s", STATE_PATH)
        return None

    session_date = data.get("_session_date", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if session_date != today:
        log.info(
            "Kill switch state is from %s (today is %s) — discarding stale state",
            session_date,
            today,
        )
        return None

    log.info(
        "Kill switch state loaded from %s (saved at %s)",
        STATE_PATH,
        data.get("_saved_at", "unknown"),
    )
    return data
