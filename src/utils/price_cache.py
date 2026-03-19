"""Persist and reload last-known prices across sessions.

Eliminates $100 synthetic-seed contamination by saving real closing
prices at shutdown and reloading them at next startup.

File format: ``data/last_known_prices.json``
    {"_saved_at": "2025-06-...T...", "prices": {"SPY": 521.3, ...}}
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PRICE_CACHE_PATH = Path(
    os.environ.get("TL_PRICE_CACHE", "data/last_known_prices.json")
)

_STALE_HOURS = 48  # warn if cache is older than this


def save_prices(prices: dict[str, float]) -> None:
    """Atomically write *prices* dict to disk with an ISO-8601 timestamp."""
    if not prices:
        log.warning("save_prices called with empty dict — skipping")
        return

    PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "_saved_at": datetime.now(timezone.utc).isoformat(),
        "prices": {k: round(v, 2) for k, v in prices.items()},
    }

    # Atomic write: write to temp file then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(PRICE_CACHE_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, str(PRICE_CACHE_PATH))
        log.info(
            "save_prices wrote %d symbols to %s", len(prices), PRICE_CACHE_PATH
        )
    except Exception:
        log.exception("save_prices failed")
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def load_prices() -> dict[str, float]:
    """Load cached prices from disk.  Returns empty dict on any error."""
    if not PRICE_CACHE_PATH.exists():
        log.info("No price cache found at %s — starting fresh", PRICE_CACHE_PATH)
        return {}

    try:
        data = json.loads(PRICE_CACHE_PATH.read_text())
        prices = data.get("prices", {})
        saved_at = data.get("_saved_at", "")

        # Staleness check
        if saved_at:
            try:
                ts = datetime.fromisoformat(saved_at)
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if age_h > _STALE_HOURS:
                    log.warning(
                        "Price cache is %.1f hours old (saved %s) — prices may be stale",
                        age_h,
                        saved_at,
                    )
                else:
                    log.info(
                        "Price cache age: %.1f hours (saved %s)", age_h, saved_at
                    )
            except (ValueError, TypeError):
                log.warning("Could not parse cache timestamp: %s", saved_at)

        log.info(
            "Loaded %d cached prices from %s", len(prices), PRICE_CACHE_PATH
        )
        return {k: float(v) for k, v in prices.items()}

    except Exception:
        log.exception("Failed to load price cache from %s", PRICE_CACHE_PATH)
        return {}
