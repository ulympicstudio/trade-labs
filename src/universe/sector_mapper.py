"""
Universe Sector Mapper — fast symbol→sector/industry lookup from universe_master.csv.

Loads ``universe_master.csv`` once at import time and caches the full mapping
in memory.  All lookups are O(1) dict access.

If the CSV cannot be found at runtime the module falls back to the legacy
``src.data.sector_map`` so existing behaviour is preserved.

Public API
----------
``get_sector(symbol)``             → sector name or "UNKNOWN"
``get_industry(symbol)``           → industry name or "UNKNOWN"
``get_subindustry(symbol)``        → subindustry name or ""
``get_sector_symbols(sector)``     → list[str]  symbols in that sector
``get_industry_symbols(industry)`` → list[str]  symbols in that industry
``get_all_sectors()``              → sorted list of sector names
``get_all_industries()``           → sorted list of industry names
``get_symbol_profile(symbol)``     → UniverseProfile dataclass (full row)
``classify_symbol(symbol)``        → SectorProfile (back-compat with sector_map)
``all_symbols()``                  → list of every symbol in the universe
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from src.data.sector_map import (
    SectorProfile,
    classify_symbol as _legacy_classify,
    SECTOR_ETFS,
)
from src.monitoring.logger import get_logger

log = get_logger("sector_mapper")

# ── Dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UniverseProfile:
    """Full row from universe_master.csv."""

    symbol: str = ""
    company_name: str = ""
    sector: str = "UNKNOWN"
    industry: str = "UNKNOWN"
    subindustry: str = ""
    sector_etf: str = ""
    industry_etf: str = ""
    avg_daily_volume: int = 0
    market_cap_bucket: str = ""   # mega / large / mid / small
    liquidity_tier: int = 1


# ── Internal caches (populated once at module load) ──────────────────

_profiles: Dict[str, UniverseProfile] = {}
_sector_to_symbols: Dict[str, List[str]] = {}
_industry_to_symbols: Dict[str, List[str]] = {}
_all_sectors: List[str] = []
_all_industries: List[str] = []
_loaded: bool = False


def _load() -> None:
    """Parse universe_master.csv and populate all caches."""
    global _loaded
    if _loaded:
        return

    csv_path = Path(__file__).parent / "universe_master.csv"
    if not csv_path.exists():
        log.warning("universe_master.csv not found at %s — falling back to legacy sector_map", csv_path)
        _loaded = True
        return

    sectors_set: set[str] = set()
    industries_set: set[str] = set()

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            sym = row.get("symbol", "").strip().upper()
            if not sym:
                continue
            vol_raw = row.get("avg_daily_volume", "0").strip()
            tier_raw = row.get("liquidity_tier", "1").strip()
            prof = UniverseProfile(
                symbol=sym,
                company_name=row.get("company_name", "").strip(),
                sector=row.get("sector", "UNKNOWN").strip(),
                industry=row.get("industry", "UNKNOWN").strip(),
                subindustry=row.get("subindustry", "").strip(),
                sector_etf=row.get("sector_etf", "").strip(),
                industry_etf=row.get("industry_etf", "").strip(),
                avg_daily_volume=int(vol_raw) if vol_raw.isdigit() else 0,
                market_cap_bucket=row.get("market_cap_bucket", "").strip(),
                liquidity_tier=int(tier_raw) if tier_raw.isdigit() else 1,
            )
            _profiles[sym] = prof

            # Build reverse maps
            sec = prof.sector
            ind = prof.industry
            _sector_to_symbols.setdefault(sec, []).append(sym)
            _industry_to_symbols.setdefault(ind, []).append(sym)
            sectors_set.add(sec)
            industries_set.add(ind)

    _all_sectors.extend(sorted(sectors_set - {"UNKNOWN"}))
    _all_industries.extend(sorted(industries_set - {"UNKNOWN"}))
    _loaded = True
    log.info(
        "universe_loaded symbols=%d sectors=%d industries=%d path=%s",
        len(_profiles), len(_all_sectors), len(_all_industries), csv_path,
    )


# Eager-load at import time
_load()


# ── Public API ───────────────────────────────────────────────────────

def get_sector(symbol: str) -> str:
    """Return the sector for *symbol*, or ``'UNKNOWN'``."""
    prof = _profiles.get(symbol.upper())
    if prof is not None:
        return prof.sector
    return _legacy_classify(symbol).sector


def get_industry(symbol: str) -> str:
    """Return the industry for *symbol*, or ``'UNKNOWN'``."""
    prof = _profiles.get(symbol.upper())
    if prof is not None:
        return prof.industry
    return _legacy_classify(symbol).industry


def get_subindustry(symbol: str) -> str:
    """Return the subindustry for *symbol*, or ``''``."""
    prof = _profiles.get(symbol.upper())
    return prof.subindustry if prof is not None else ""


def get_sector_symbols(sector: str) -> List[str]:
    """Return all symbols in *sector*."""
    return list(_sector_to_symbols.get(sector, []))


def get_industry_symbols(industry: str) -> List[str]:
    """Return all symbols in *industry*."""
    return list(_industry_to_symbols.get(industry, []))


def get_all_sectors() -> List[str]:
    """Return sorted list of all known sector names."""
    return list(_all_sectors)


def get_all_industries() -> List[str]:
    """Return sorted list of all known industry names."""
    return list(_all_industries)


def get_symbol_profile(symbol: str) -> UniverseProfile:
    """Return the full UniverseProfile, or a default UNKNOWN profile."""
    return _profiles.get(symbol.upper(), UniverseProfile(symbol=symbol.upper()))


def classify_symbol(symbol: str) -> SectorProfile:
    """Back-compatible wrapper: return a SectorProfile from the universe.

    Falls back to the legacy sector_map if the symbol is not in the universe.
    """
    prof = _profiles.get(symbol.upper())
    if prof is not None:
        return SectorProfile(
            sector=prof.sector,
            industry=prof.industry,
            etf=prof.sector_etf,
        )
    return _legacy_classify(symbol)


def all_symbols() -> List[str]:
    """Return every symbol loaded from the universe."""
    return list(_profiles.keys())
