"""Market session helpers.

Provides :func:`get_us_equity_session` which returns the current US
equity market session based on America/New_York local time.

Sessions
--------
- **OFF_HOURS** — 20:00–04:00 ET
- **PREMARKET** — 04:00–09:30 ET
- **RTH** — 09:30–16:00 ET  (Regular Trading Hours)
- **AFTERHOURS** — 16:00–20:00 ET
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Optional

_log = logging.getLogger("session")

# ── Force-session override (for dev / testing) ──────────────────────
# FORCE_SESSION: hard override — changes the session returned by
# get_us_equity_session() for ALL arms (backward compat).
_FORCE_SESSION = os.environ.get("FORCE_SESSION", "").upper().strip()

# TL_TEST_FORCE_SESSION: pipeline-gate override — bypasses specific
# session gates (risk blueprint gate, execution session gate) without
# changing the session that signal/ingest see.  This lets signal keep
# its natural OFF_HOURS / PREMARKET flow while unblocking downstream.
_TEST_FORCE_SESSION = os.environ.get(
    "TL_TEST_FORCE_SESSION", ""
).upper().strip()

# ── Session constants ────────────────────────────────────────────────
OFF_HOURS = "OFF_HOURS"
PREMARKET = "PREMARKET"
RTH = "RTH"
AFTERHOURS = "AFTERHOURS"

# Eastern Time offset (fixed; see _to_et for DST handling).
_ET_STANDARD_OFFSET = timedelta(hours=-5)   # EST
_ET_DST_OFFSET = timedelta(hours=-4)        # EDT

# Session boundaries in ET local time (hour, minute)
_PRE_OPEN = dtime(4, 0)    # 04:00  premarket opens
_RTH_OPEN = dtime(9, 30)   # 09:30  regular open
_RTH_CLOSE = dtime(16, 0)  # 16:00  regular close
_AH_CLOSE = dtime(20, 0)   # 20:00  afterhours close


def _to_et(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime to US Eastern time, respecting DST.

    Uses the standard US rule:
      - EDT (UTC-4)  2nd Sunday in March  02:00  →  1st Sunday in November  02:00
      - EST (UTC-5)  otherwise

    This avoids a ``zoneinfo`` / ``pytz`` dependency.
    """
    year = utc_dt.year
    # 2nd Sunday in March
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)  # 2nd Sun
    dst_start = dst_start.replace(hour=7)  # 02:00 ET = 07:00 UTC

    # 1st Sunday in November
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)  # 1st Sun
    dst_end = dst_end.replace(hour=6)  # 02:00 ET = 06:00 UTC

    if dst_start <= utc_dt < dst_end:
        offset = _ET_DST_OFFSET
    else:
        offset = _ET_STANDARD_OFFSET

    return utc_dt + offset


def get_us_equity_session(now_utc: Optional[datetime] = None) -> str:
    """Return the current US equity session label.

    Parameters
    ----------
    now_utc:
        An aware UTC datetime.  Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    str
        One of ``OFF_HOURS``, ``PREMARKET``, ``RTH``, ``AFTERHOURS``.
    """
    if _FORCE_SESSION in (OFF_HOURS, PREMARKET, RTH, AFTERHOURS):
        _log.debug(
            "forced_session_override requested=%s effective=%s source=env",
            _FORCE_SESSION, _FORCE_SESSION,
        )
        return _FORCE_SESSION

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    et = _to_et(now_utc)
    t = et.time()

    if t < _PRE_OPEN:
        return OFF_HOURS
    if t < _RTH_OPEN:
        return PREMARKET
    if t < _RTH_CLOSE:
        return RTH
    if t < _AH_CLOSE:
        return AFTERHOURS
    return OFF_HOURS


def is_test_session_forced() -> bool:
    """Return True when TL_TEST_FORCE_SESSION is set to a valid session.

    Arms should call this to decide whether to relax session-specific
    gates (e.g. risk blueprint gate, execution session gate) during
    dev / testing.  Does NOT affect the session seen by signal/ingest.
    """
    return _TEST_FORCE_SESSION in (OFF_HOURS, PREMARKET, RTH, AFTERHOURS)


def get_test_force_session() -> str:
    """Return the TL_TEST_FORCE_SESSION value, or empty string."""
    if _TEST_FORCE_SESSION in (OFF_HOURS, PREMARKET, RTH, AFTERHOURS):
        return _TEST_FORCE_SESSION
    return ""
