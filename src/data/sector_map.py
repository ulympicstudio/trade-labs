"""
Sector Map — static symbol → sector / industry / benchmark ETF classification.

Every symbol is classified into a sector, industry, and benchmark ETF.
Symbols not found in the map return an UNKNOWN profile so downstream
code can treat them as zero-contribution without special-casing.

Usage::

    from src.data.sector_map import classify_symbol, SectorProfile

    p = classify_symbol("AAPL")
    # p.sector == "Technology"
    # p.industry == "Consumer Electronics"
    # p.etf == "XLK"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class SectorProfile:
    """Classification result for a single symbol."""

    sector: str = "UNKNOWN"
    industry: str = "UNKNOWN"
    etf: str = ""                   # benchmark sector ETF (e.g. XLK, XLF)


# ── Sector ETF mapping ──────────────────────────────────────────────

SECTOR_ETFS: Dict[str, str] = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financials": "XLF",
    "Energy": "XLE",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

_UNKNOWN = SectorProfile()

# ── Static symbol → (sector, industry) table ─────────────────────────
# Covers actively traded large/mid-cap names plus common ETFs.

_MAP: Dict[str, SectorProfile] = {
    # ── Technology ────────────────────────────────────────────────────
    "AAPL": SectorProfile("Technology", "Consumer Electronics", "XLK"),
    "MSFT": SectorProfile("Technology", "Software", "XLK"),
    "NVDA": SectorProfile("Technology", "Semiconductors", "XLK"),
    "AMD": SectorProfile("Technology", "Semiconductors", "XLK"),
    "INTC": SectorProfile("Technology", "Semiconductors", "XLK"),
    "AVGO": SectorProfile("Technology", "Semiconductors", "XLK"),
    "QCOM": SectorProfile("Technology", "Semiconductors", "XLK"),
    "TSM": SectorProfile("Technology", "Semiconductors", "XLK"),
    "MU": SectorProfile("Technology", "Semiconductors", "XLK"),
    "MRVL": SectorProfile("Technology", "Semiconductors", "XLK"),
    "ARM": SectorProfile("Technology", "Semiconductors", "XLK"),
    "TXN": SectorProfile("Technology", "Semiconductors", "XLK"),
    "LRCX": SectorProfile("Technology", "Semiconductor Equipment", "XLK"),
    "AMAT": SectorProfile("Technology", "Semiconductor Equipment", "XLK"),
    "KLAC": SectorProfile("Technology", "Semiconductor Equipment", "XLK"),
    "ASML": SectorProfile("Technology", "Semiconductor Equipment", "XLK"),
    "ORCL": SectorProfile("Technology", "Software", "XLK"),
    "CRM": SectorProfile("Technology", "Software", "XLK"),
    "ADBE": SectorProfile("Technology", "Software", "XLK"),
    "NOW": SectorProfile("Technology", "Software", "XLK"),
    "INTU": SectorProfile("Technology", "Software", "XLK"),
    "PANW": SectorProfile("Technology", "Cybersecurity", "XLK"),
    "CRWD": SectorProfile("Technology", "Cybersecurity", "XLK"),
    "FTNT": SectorProfile("Technology", "Cybersecurity", "XLK"),
    "ZS": SectorProfile("Technology", "Cybersecurity", "XLK"),
    "CSCO": SectorProfile("Technology", "Networking", "XLK"),
    "IBM": SectorProfile("Technology", "IT Services", "XLK"),
    "PLTR": SectorProfile("Technology", "Software", "XLK"),
    "SNOW": SectorProfile("Technology", "Software", "XLK"),
    "NET": SectorProfile("Technology", "Cloud Infrastructure", "XLK"),
    "DDOG": SectorProfile("Technology", "Software", "XLK"),
    "DELL": SectorProfile("Technology", "Hardware", "XLK"),
    "HPQ": SectorProfile("Technology", "Hardware", "XLK"),
    "HPE": SectorProfile("Technology", "Hardware", "XLK"),
    "SMCI": SectorProfile("Technology", "Hardware", "XLK"),
    "SHOP": SectorProfile("Technology", "E-Commerce Software", "XLK"),
    "SQ": SectorProfile("Technology", "Fintech", "XLK"),
    "MSTR": SectorProfile("Technology", "Software", "XLK"),

    # ── Healthcare ───────────────────────────────────────────────────
    "JNJ": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "UNH": SectorProfile("Healthcare", "Health Insurance", "XLV"),
    "LLY": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "NVO": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "ABBV": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "PFE": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "MRK": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "ABT": SectorProfile("Healthcare", "Medical Devices", "XLV"),
    "TMO": SectorProfile("Healthcare", "Life Sciences", "XLV"),
    "AMGN": SectorProfile("Healthcare", "Biotechnology", "XLV"),
    "GILD": SectorProfile("Healthcare", "Biotechnology", "XLV"),
    "BIIB": SectorProfile("Healthcare", "Biotechnology", "XLV"),
    "MRNA": SectorProfile("Healthcare", "Biotechnology", "XLV"),
    "ISRG": SectorProfile("Healthcare", "Medical Devices", "XLV"),
    "MDT": SectorProfile("Healthcare", "Medical Devices", "XLV"),
    "DHR": SectorProfile("Healthcare", "Life Sciences", "XLV"),
    "BMY": SectorProfile("Healthcare", "Pharmaceuticals", "XLV"),
    "REGN": SectorProfile("Healthcare", "Biotechnology", "XLV"),
    "VRTX": SectorProfile("Healthcare", "Biotechnology", "XLV"),

    # ── Financials ───────────────────────────────────────────────────
    "JPM": SectorProfile("Financials", "Banks", "XLF"),
    "BAC": SectorProfile("Financials", "Banks", "XLF"),
    "WFC": SectorProfile("Financials", "Banks", "XLF"),
    "GS": SectorProfile("Financials", "Investment Banking", "XLF"),
    "MS": SectorProfile("Financials", "Investment Banking", "XLF"),
    "C": SectorProfile("Financials", "Banks", "XLF"),
    "BRK.B": SectorProfile("Financials", "Insurance", "XLF"),
    "V": SectorProfile("Financials", "Payments", "XLF"),
    "MA": SectorProfile("Financials", "Payments", "XLF"),
    "AXP": SectorProfile("Financials", "Payments", "XLF"),
    "PYPL": SectorProfile("Financials", "Fintech", "XLF"),
    "SCHW": SectorProfile("Financials", "Brokerage", "XLF"),
    "BLK": SectorProfile("Financials", "Asset Management", "XLF"),
    "COF": SectorProfile("Financials", "Consumer Finance", "XLF"),
    "USB": SectorProfile("Financials", "Banks", "XLF"),

    # ── Energy ───────────────────────────────────────────────────────
    "XOM": SectorProfile("Energy", "Oil & Gas", "XLE"),
    "CVX": SectorProfile("Energy", "Oil & Gas", "XLE"),
    "COP": SectorProfile("Energy", "Oil & Gas", "XLE"),
    "SLB": SectorProfile("Energy", "Oilfield Services", "XLE"),
    "EOG": SectorProfile("Energy", "Oil & Gas", "XLE"),
    "MPC": SectorProfile("Energy", "Refining", "XLE"),
    "PSX": SectorProfile("Energy", "Refining", "XLE"),
    "VLO": SectorProfile("Energy", "Refining", "XLE"),
    "OXY": SectorProfile("Energy", "Oil & Gas", "XLE"),
    "HAL": SectorProfile("Energy", "Oilfield Services", "XLE"),

    # ── Consumer Discretionary ───────────────────────────────────────
    "AMZN": SectorProfile("Consumer Discretionary", "E-Commerce", "XLY"),
    "TSLA": SectorProfile("Consumer Discretionary", "Electric Vehicles", "XLY"),
    "HD": SectorProfile("Consumer Discretionary", "Home Improvement", "XLY"),
    "NKE": SectorProfile("Consumer Discretionary", "Apparel", "XLY"),
    "MCD": SectorProfile("Consumer Discretionary", "Restaurants", "XLY"),
    "SBUX": SectorProfile("Consumer Discretionary", "Restaurants", "XLY"),
    "LOW": SectorProfile("Consumer Discretionary", "Home Improvement", "XLY"),
    "TJX": SectorProfile("Consumer Discretionary", "Retail", "XLY"),
    "BKNG": SectorProfile("Consumer Discretionary", "Travel", "XLY"),
    "CMG": SectorProfile("Consumer Discretionary", "Restaurants", "XLY"),
    "GM": SectorProfile("Consumer Discretionary", "Automobiles", "XLY"),
    "F": SectorProfile("Consumer Discretionary", "Automobiles", "XLY"),
    "RIVN": SectorProfile("Consumer Discretionary", "Electric Vehicles", "XLY"),
    "LCID": SectorProfile("Consumer Discretionary", "Electric Vehicles", "XLY"),

    # ── Consumer Staples ─────────────────────────────────────────────
    "PG": SectorProfile("Consumer Staples", "Household Products", "XLP"),
    "KO": SectorProfile("Consumer Staples", "Beverages", "XLP"),
    "PEP": SectorProfile("Consumer Staples", "Beverages", "XLP"),
    "COST": SectorProfile("Consumer Staples", "Retail", "XLP"),
    "WMT": SectorProfile("Consumer Staples", "Retail", "XLP"),
    "PM": SectorProfile("Consumer Staples", "Tobacco", "XLP"),
    "MO": SectorProfile("Consumer Staples", "Tobacco", "XLP"),
    "CL": SectorProfile("Consumer Staples", "Household Products", "XLP"),
    "MDLZ": SectorProfile("Consumer Staples", "Food", "XLP"),
    "STZ": SectorProfile("Consumer Staples", "Beverages", "XLP"),

    # ── Industrials ──────────────────────────────────────────────────
    "CAT": SectorProfile("Industrials", "Machinery", "XLI"),
    "BA": SectorProfile("Industrials", "Aerospace & Defense", "XLI"),
    "HON": SectorProfile("Industrials", "Diversified", "XLI"),
    "UPS": SectorProfile("Industrials", "Logistics", "XLI"),
    "FDX": SectorProfile("Industrials", "Logistics", "XLI"),
    "RTX": SectorProfile("Industrials", "Aerospace & Defense", "XLI"),
    "LMT": SectorProfile("Industrials", "Aerospace & Defense", "XLI"),
    "NOC": SectorProfile("Industrials", "Aerospace & Defense", "XLI"),
    "GD": SectorProfile("Industrials", "Aerospace & Defense", "XLI"),
    "GE": SectorProfile("Industrials", "Diversified", "XLI"),
    "MMM": SectorProfile("Industrials", "Diversified", "XLI"),
    "DE": SectorProfile("Industrials", "Machinery", "XLI"),
    "WM": SectorProfile("Industrials", "Waste Management", "XLI"),

    # ── Materials ────────────────────────────────────────────────────
    "LIN": SectorProfile("Materials", "Chemicals", "XLB"),
    "APD": SectorProfile("Materials", "Chemicals", "XLB"),
    "FCX": SectorProfile("Materials", "Mining", "XLB"),
    "NEM": SectorProfile("Materials", "Mining", "XLB"),
    "DOW": SectorProfile("Materials", "Chemicals", "XLB"),
    "NUE": SectorProfile("Materials", "Steel", "XLB"),
    "AA": SectorProfile("Materials", "Aluminum", "XLB"),
    "GOLD": SectorProfile("Materials", "Mining", "XLB"),

    # ── Real Estate ──────────────────────────────────────────────────
    "AMT": SectorProfile("Real Estate", "REITs", "XLRE"),
    "PLD": SectorProfile("Real Estate", "REITs", "XLRE"),
    "CCI": SectorProfile("Real Estate", "REITs", "XLRE"),
    "PSA": SectorProfile("Real Estate", "REITs", "XLRE"),
    "EQIX": SectorProfile("Real Estate", "Data Center REITs", "XLRE"),
    "O": SectorProfile("Real Estate", "REITs", "XLRE"),
    "SPG": SectorProfile("Real Estate", "REITs", "XLRE"),

    # ── Utilities ────────────────────────────────────────────────────
    "NEE": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "DUK": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "SO": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "D": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "AEP": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "SRE": SectorProfile("Utilities", "Electric Utilities", "XLU"),
    "EXC": SectorProfile("Utilities", "Electric Utilities", "XLU"),

    # ── Communication Services ───────────────────────────────────────
    "GOOGL": SectorProfile("Communication Services", "Internet", "XLC"),
    "GOOG": SectorProfile("Communication Services", "Internet", "XLC"),
    "META": SectorProfile("Communication Services", "Social Media", "XLC"),
    "NFLX": SectorProfile("Communication Services", "Streaming", "XLC"),
    "DIS": SectorProfile("Communication Services", "Entertainment", "XLC"),
    "CMCSA": SectorProfile("Communication Services", "Media", "XLC"),
    "T": SectorProfile("Communication Services", "Telecom", "XLC"),
    "VZ": SectorProfile("Communication Services", "Telecom", "XLC"),
    "TMUS": SectorProfile("Communication Services", "Telecom", "XLC"),
    "SNAP": SectorProfile("Communication Services", "Social Media", "XLC"),
    "PINS": SectorProfile("Communication Services", "Social Media", "XLC"),
    "ROKU": SectorProfile("Communication Services", "Streaming", "XLC"),
    "SPOT": SectorProfile("Communication Services", "Streaming", "XLC"),

    # ── Broad-market / Index ETFs (sector = "Index") ─────────────────
    "SPY": SectorProfile("Index", "Broad Market", "SPY"),
    "QQQ": SectorProfile("Index", "Nasdaq 100", "QQQ"),
    "IWM": SectorProfile("Index", "Russell 2000", "IWM"),
    "DIA": SectorProfile("Index", "Dow 30", "DIA"),
    "VTI": SectorProfile("Index", "Total Market", "VTI"),
    "VOO": SectorProfile("Index", "S&P 500", "VOO"),
    "IVV": SectorProfile("Index", "S&P 500", "IVV"),

    # ── Sector ETFs (sector mirrors the sector they track) ───────────
    "XLK": SectorProfile("Technology", "Sector ETF", "XLK"),
    "XLV": SectorProfile("Healthcare", "Sector ETF", "XLV"),
    "XLF": SectorProfile("Financials", "Sector ETF", "XLF"),
    "XLE": SectorProfile("Energy", "Sector ETF", "XLE"),
    "XLY": SectorProfile("Consumer Discretionary", "Sector ETF", "XLY"),
    "XLP": SectorProfile("Consumer Staples", "Sector ETF", "XLP"),
    "XLI": SectorProfile("Industrials", "Sector ETF", "XLI"),
    "XLB": SectorProfile("Materials", "Sector ETF", "XLB"),
    "XLRE": SectorProfile("Real Estate", "Sector ETF", "XLRE"),
    "XLU": SectorProfile("Utilities", "Sector ETF", "XLU"),
    "XLC": SectorProfile("Communication Services", "Sector ETF", "XLC"),
}


def classify_symbol(symbol: str) -> SectorProfile:
    """Return the SectorProfile for *symbol*, or UNKNOWN if not mapped."""
    return _MAP.get(symbol.upper(), _UNKNOWN)


def all_sectors() -> list[str]:
    """Return a sorted list of all known sector names (excl. UNKNOWN)."""
    return sorted({p.sector for p in _MAP.values() if p.sector != "UNKNOWN"})


def symbols_in_sector(sector: str) -> list[str]:
    """Return all symbols mapped to *sector*."""
    return [sym for sym, p in _MAP.items() if p.sector == sector]
