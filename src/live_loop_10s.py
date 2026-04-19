from dotenv import load_dotenv
load_dotenv()

import time
import os
import json
import math
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Set, List, Dict, Tuple, Optional

import requests
from ib_insync import IB, Stock, util, MarketOrder
from src.data.ib_market_data import make_contract as _make_ib_contract

from config.identity import SYSTEM_NAME, HUMAN_NAME
from config.runtime import is_armed, execution_backend, is_paper, get_resolved_config
from config.ib_config import IB_HOST, IB_PORT
import os as _os
IB_CLIENT_ID = int(_os.getenv("TL_LIVELOOP_IB_CLIENT_ID", "10"))
from config.universe_filter import ALLOWED_SEC_TYPES, ALLOWED_EXCHANGES, STOCK_ALLOWLIST, STOCK_BLOCKLIST, ETF_KEYWORDS

from src.execution.bracket_orders import (
    BracketParams,
    place_limit_tp_trail_bracket,
    place_trailing_stop,
)
from src.signals.market_scanner import (
    coarse_symbol_allowed,
    scan_us_most_active_stocks,
    scanner_mark_restart,
    scanner_note_request,
    scanner_note_request_end,
    scanner_note_stale_callback_ignored,
    scanner_reset_generation,
    scanner_session_state,
    scanner_stability_counters,
)
from src.signals.candidate_pool import CandidatePool
from src.signals.scan_rotator import ScanRotator
from src.signals.score_candidates import score_scan_results
from src.risk.daily_pnl_manager import (
    record_session_start_equity, is_kill_switch_active, get_kill_switch_status
)
from src.risk.regime import get_regime
from src.signals.signal_validator import (
    compute_candidate_metrics,
    passes_hyper_swing_filters,
    CandidateMetrics,
    fetch_spy_5m,
)
from src.quant.hyper_swing_filters import calc_momentum
from src.analysis.signal_distribution import SignalDistributionAnalyzer
from src.analysis.order_lifecycle import LifecycleLogger, OrderEvent
from src.analysis.trade_journal import TradeJournal
from src.analysis.dashboard_snapshot import DashboardSnapshot
import logging

log = logging.getLogger("paper_session")

from config.risk_limits import (
    MIN_UNIFIED_SCORE,
    MIN_ADV20_DOLLARS,
    MIN_ATR_PCT,
    MIN_VOLUME_ACCEL,
    MIN_RS_VS_SPY,
    PRICE_MIN as CFG_PRICE_MIN,
    PRICE_MAX as CFG_PRICE_MAX,
    PRICE_MAX_ALLOWLIST,
)

# ====== CATALYST-DRIVEN TRADING ENGINE ======
try:
    from src.data.catalyst_hunter import CatalystHunter
    from src.data.catalyst_scorer import CatalystScorer
    from src.data.research_engine import ResearchEngine
    CATALYST_ENGINE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Catalyst engine not available: {e}")
    CATALYST_ENGINE_AVAILABLE = False


# ---- Risk Framework ----
BASE_RISK_PER_TRADE = 0.005
CONVICTION_RISK_PER_TRADE = 0.0075
BASE_MAX_TOTAL_OPEN_RISK = 0.02
CONVICTION_MAX_TOTAL_OPEN_RISK = 0.045
BASE_MAX_CONCURRENT_POSITIONS = 4
CONVICTION_MAX_CONCURRENT_POSITIONS = 6
MIN_CATALYST_SCORE = float(os.getenv("TL_MIN_CATALYST_SCORE", "60"))  # Canonical score threshold

# Phase 2: Unified score weights
UNIFIED_CATALYST_WEIGHT = 0.60
UNIFIED_QUANT_WEIGHT    = 0.40
# Regime-based risk scaling
YELLOW_RISK_REDUCTION   = 0.75   # 25% risk haircut in YELLOW regime
YELLOW_POSITION_PENALTY = 1      # Reduce max positions by 1

LOSS_STREAK_LOOKBACK = 2
LOSS_STREAK_PENALTY_TRADES = 3
LOSS_STREAK_RISK_REDUCTION = 0.75

PEAK_DRAWDOWN_THRESHOLD = 0.05
PEAK_DRAWDOWN_RISK_PCT = 0.004
# ---- Loop Settings ----
LOOP_SECONDS = 10
SCAN_REFRESH_SECONDS = 300
SCAN_LIMIT = 60
SCORE_TOP_N_FROM_SCAN = 20
TRADE_TOP_N = 12  # Top N scored candidates to evaluate (was 6, show more variety)

# ---- CandidatePool settings ----
REFILL_THRESHOLD = 40   # refill pool when it drops below this
BATCH_SIZE = 25         # symbols popped per loop iteration
SCANNER_SCORE_INTERVAL_SECONDS = 60  # min seconds between scanner scoring runs
CATALYST_TOP_N = 8
MIN_CATALYSTS_FOR_SCAN = 3
CATALYST_POOL_SIZE = 20

ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.5  # How many ATRs below entry is our hard stop
TRAIL_ATR_MULT = 1.2
TRAIL_ACTIVATE_ATR = 1.5
TRAIL_CHECK_SECONDS = 30

MIN_PRICE = 2.0
PRINT_HEARTBEAT_SECONDS = 60

# Cache ATR so we don’t request daily bars repeatedly
ATR_CACHE_SECONDS = 600  # 10 minutes
# Market breadth cache
BREADTH_CACHE_SECONDS = 300
BREADTH_ADV_THRESHOLD = 0.65
BREADTH_SCAN_LIMIT = 15
# Trade history for loss-streak tracking
TRADES_FILE = Path("data/trade_history/trades.json")
# ---- Bracket Throttling & Safety ----
MAX_NEW_BRACKETS_PER_LOOP = 2           # Submit max 2 brackets per 10s loop
COOLDOWN_SECONDS_PER_SYMBOL = 300       # Don't retry same symbol for 5 min
BRACKET_COOLDOWN_SECONDS = int(os.getenv("TRADE_LABS_BRACKET_COOLDOWN_SECONDS", "20"))
HYPER_RECHECK_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_SECONDS", "30"))
HYPER_RECHECK_VOL_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_VOL_SECONDS", str(HYPER_RECHECK_SECONDS)))
HYPER_RECHECK_VWAP_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_VWAP_SECONDS", str(HYPER_RECHECK_SECONDS)))
HYPER_RECHECK_PRICECAP_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_PRICECAP_SECONDS", "900"))
ENABLE_BOUNCE_MODE = os.getenv("TRADE_LABS_ENABLE_BOUNCE_MODE", "1") == "1"
BOUNCE_MIN_PLAYBOOK_WIN_RATE = float(os.getenv("TRADE_LABS_BOUNCE_MIN_WIN_RATE", "0.0"))
BOUNCE_MIN_PLAYBOOK_EXPECTANCY = float(os.getenv("TRADE_LABS_BOUNCE_MIN_EXPECTANCY", "-0.005"))
BOUNCE_MIN_PLAYBOOK_SCORE = float(os.getenv("TRADE_LABS_BOUNCE_MIN_SCORE", "25"))
BOUNCE_MAX_MAE_ABS = float(os.getenv("TRADE_LABS_BOUNCE_MAX_MAE_ABS", "0.09"))
MIN_BOUNCE_SAMPLE_SIZE_FLOOR = 12
BASE_MIN_UNIFIED_SCORE_FLOOR = 70.0
CONVICTION_MIN_UNIFIED_SCORE_FLOOR = 68.0
BOUNCE_MIN_UNIFIED_SCORE_FLOOR = 68.0
PAPER_MIN_VOLUME_ACCEL = float(os.getenv("TL_PAPER_MIN_VOLUME_ACCEL", "0.8"))
BOUNCE_MIN_SAMPLE_SIZE = int(os.getenv(
    "TRADE_LABS_BOUNCE_MIN_SAMPLE_SIZE", str(MIN_BOUNCE_SAMPLE_SIZE_FLOOR)
))
# Unified score thresholds: env vars can lower these for paper calibration.
# Production defaults are the *_FLOOR values above.
BASE_MIN_UNIFIED_SCORE = float(os.getenv(
    "TRADE_LABS_BASE_MIN_UNIFIED_SCORE", str(float(MIN_UNIFIED_SCORE))
))
CONVICTION_MIN_UNIFIED_SCORE = float(os.getenv(
    "TRADE_LABS_CONVICTION_MIN_UNIFIED_SCORE", "68"
))
BOUNCE_MIN_UNIFIED_SCORE = float(os.getenv(
    "TRADE_LABS_BOUNCE_MIN_UNIFIED_SCORE", "68"
))

# ── Module 1: Universe Gate ──────────────────────────────────────────
# Pre-IBKR symbol hygiene: cheap metadata rules for exchange, ticker
# shape, and known bad universes.  Runs before quarantine + contract
# qualification so obvious junk never touches IBKR at all.

# Known ETF / leveraged / inverse / commodity / crypto tickers that
# recur in IBKR scanner results.  Static deny — never tradeable by UTS.
_TICKER_DENY_SET: frozenset[str] = frozenset({
    # ── Major index ETFs ──
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP",
    "MDY", "IJR", "IJH", "IWB", "IWF", "IWD", "VTV", "VUG",
    # ── Sector / thematic ETFs ──
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLB", "XLC", "XLU",
    "XLP", "XLY", "XLRE", "GDX", "GDXJ", "KRE", "KBE",
    "SMH", "SOXX", "ARKK", "ARKG", "ARKW", "ARKF", "ARKQ",
    "XBI", "IBB", "ITB", "XHB", "TAN", "ICLN", "LIT", "HACK",
    # ── Leveraged / inverse ──
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
    "SPXL", "SPXS", "SPXU", "UPRO", "UDOW", "SDOW",
    "LABU", "LABD", "FNGU", "FNGD", "TECL", "TECS",
    "FAS", "FAZ", "ERX", "ERY", "NUGT", "DUST", "JNUG", "JDST",
    "UVXY", "SVXY", "UVIX", "VIXY", "VXX",
    "TSLL", "TSLS",
    # ── Commodity / currency / bond ──
    "GLD", "SLV", "IAU", "USO", "UNG", "KOLD", "BOIL",
    "UUP", "FXE", "FXY", "FXB", "FXA", "FXC",
    "TLT", "TBT", "TMF", "TMV", "IEF", "SHY", "BND", "AGG",
    "LQD", "HYG", "JNK", "EMB",
    # ── Crypto trusts / ETFs ──
    "BITO", "GBTC", "ETHE", "MSTR",
    # ── International / EM ETFs ──
    "EEM", "EFA", "VWO", "IEMG", "FXI", "KWEB", "MCHI",
    "EWZ", "EWJ", "EWG", "EWU", "EWY", "EWW", "EWT",
    # ── REIT / real-estate ETFs ──
    "VNQ", "IYR", "XLRE", "REM", "MORT",
    # ── Dividend / low-vol / factor ──
    "SCHD", "VIG", "DVY", "HDV", "SPHD", "JEPI", "JEPQ",
})

# Ticker shape: reject preferred shares (trailing digits like ZIONP),
# warrants (trailing W/WS), units (trailing U), and rights.
_RE_BAD_TICKER = re.compile(
    r"(?:"
    r"^.{1,4}[PW]$"       # preferred / warrant suffix  (ZIONP, ACAHW)
    r"|^.+WS$"            # warrant-suffix variant      (ACAHWS)
    r"|^.{1,4}U$"         # unit tickers                (ACACU)
    r"|^.{1,4}R$"         # rights                      (ACACR)
    r")",
    re.I,
)

# OTC / PINK exchange deny list — these should never reach qualification
_OTC_EXCHANGES: frozenset[str] = frozenset({
    "PINK", "OTC", "OTCBB", "GREY", "OTCQX", "OTCQB",
    "OTCMKTS", "PINKSHEETS",
})


def _filter_valid_candidates(
    symbols: list,
    ib: IB,
    min_score: float,
    invalid_symbols: set,
    valid_contracts: dict,
) -> list:
    """Pre-filter candidates: universe gate + IB contract check + score threshold.

    Returns only candidates that pass all validation gates.  This helper
    consolidates the repeated gate/qualify/score logic used for both
    catalyst and scanner candidate paths.
    """
    valid = []
    for cand in symbols:
        sym = cand.symbol if hasattr(cand, "symbol") else str(cand)
        if sym in invalid_symbols:
            continue
        gate_ok, gate_reason = _universe_gate(sym)
        if not gate_ok:
            invalid_symbols.add(sym)
            continue
        if not coarse_symbol_allowed(sym):
            invalid_symbols.add(sym)
            continue
        cat_score = getattr(cand, "catalyst_score", None) or 0.0
        if cat_score < min_score:
            continue
        ok, c, reason = _safe_qualify_contract(ib, sym, valid_contracts)
        if not ok:
            if reason != "broker_fault":
                invalid_symbols.add(sym)
            continue
        valid.append(cand)
    return valid


def _universe_gate(symbol: str, exchange: str = "") -> tuple[bool, str]:
    """Cheap pre-IBKR symbol gate.  No network calls.

    Returns (ok, reason).  reason is empty string on pass.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return False, "empty"
    if s in _TICKER_DENY_SET:
        return False, "denied_ticker"
    if _RE_BAD_TICKER.match(s):
        return False, "bad_ticker_shape"
    ex = (exchange or "").strip().upper()
    if ex and ex in _OTC_EXCHANGES:
        return False, "otc_exchange"
    return True, ""


# ── Module 2: Broker Fault Sentinel ──────────────────────────────────
# Separates infrastructure faults (1100/1102, timeout, KeyError) from
# symbol-quality rejects so alpha diagnostics stay clean.

_BROKER_FAULT_WINDOW = int(os.getenv("TL_BROKER_FAULT_WINDOW", "300"))   # 5 min
_BROKER_FAULT_THRESHOLD = int(os.getenv("TL_BROKER_FAULT_THRESHOLD", "5"))  # trip degraded


class _BrokerFaultSentinel:
    __slots__ = (
        "error_1100", "error_1102", "timeout", "keyerror", "other",
        "_timestamps", "degraded", "degraded_since",
    )

    def __init__(self):
        self.error_1100 = 0
        self.error_1102 = 0
        self.timeout = 0
        self.keyerror = 0
        self.other = 0
        self._timestamps: list[float] = []  # rolling window
        self.degraded = False
        self.degraded_since: float = 0.0

    @property
    def total(self) -> int:
        return self.error_1100 + self.error_1102 + self.timeout + self.keyerror + self.other

    def record(self, exc_msg: str) -> str:
        """Classify and record a broker fault. Returns fault type string."""
        msg = (exc_msg or "").lower()
        now = time.time()
        if "1100" in msg:
            self.error_1100 += 1
            fault_type = "error_1100"
        elif "1102" in msg:
            self.error_1102 += 1
            fault_type = "error_1102"
        elif "timeout" in msg:
            self.timeout += 1
            fault_type = "timeout"
        elif "keyerror" in msg:
            self.keyerror += 1
            fault_type = "keyerror"
        else:
            self.other += 1
            fault_type = "other"
        self._timestamps.append(now)
        self._check_degraded(now)
        return fault_type

    def _check_degraded(self, now: float) -> None:
        cutoff = now - _BROKER_FAULT_WINDOW
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= _BROKER_FAULT_THRESHOLD:
            if not self.degraded:
                self.degraded = True
                self.degraded_since = now
        else:
            self.degraded = False
            self.degraded_since = 0.0

    def log_line(self) -> str:
        parts = (
            f"[BROKER] faults={self.total} "
            f"1100={self.error_1100} 1102={self.error_1102} "
            f"timeout={self.timeout} keyerror={self.keyerror} "
            f"other={self.other}"
        )
        if self.degraded:
            age = int(time.time() - self.degraded_since)
            parts += f" DEGRADED({age}s)"
        return parts


_broker_sentinel = _BrokerFaultSentinel()


# ── Upstream funnel counters ─────────────────────────────────────────

_ALLOWED_PRIMARY_EXCHANGES = {
    "NASDAQ", "NMS", "NYSE", "AMEX", "ARCA", "BATS", "IEX",
    "MEMX", "EDGEA", "EDGX", "BYX",
}

_RE_BAD_NAME = [
    (re.compile(r"\bETF\b", re.I), "etf"),
    (re.compile(r"\bETN\b", re.I), "etn"),
    (re.compile(r"\bADR\b", re.I), "adr"),
    (re.compile(r"\bTRUST\b", re.I), "trust"),
    (re.compile(r"\bFUND\b", re.I), "fund"),
    (re.compile(r"\bREIT\b", re.I), "reit"),
    (re.compile(r"\bCLOSED[- ]?END\b", re.I), "fund"),
    (re.compile(r"\bLP\b", re.I), "fund"),
    (re.compile(r"\bINDEX\b", re.I), "etf"),
    (re.compile(r"\bPROSHARES\b", re.I), "etf"),
    (re.compile(r"\bULTRA\b", re.I), "etf"),
    (re.compile(r"\b[23]X\b", re.I), "etf"),
    (re.compile(r"\bLEVERAGED\b", re.I), "etf"),
    (re.compile(r"\bINVERSE\b", re.I), "etf"),
    (re.compile(r"\bNOTES?\b", re.I), "etn"),
    (re.compile(r"\bSECURITIES\b", re.I), "etf"),
]


class _FunnelCounters:
    __slots__ = (
        "scan_raw", "gate_rejected", "deduped",
        "static_qualified", "static_rejected",
        "contract_ok", "contract_rejected", "broker_faults",
        "quarantined",
        "rej_etf", "rej_adr", "rej_trust", "rej_reit",
        "rej_etn", "rej_fund", "rej_non_common", "rej_bad_exchange",
        "rej_contract_invalid", "rej_contract_timeout",
        "rej_denied_ticker", "rej_bad_ticker_shape", "rej_otc_exchange",
    )

    def __init__(self):
        self.reset()

    def reset(self, raw: int = 0):
        self.scan_raw = raw
        self.gate_rejected = 0
        self.deduped = 0
        self.static_qualified = 0
        self.static_rejected = 0
        self.contract_ok = 0
        self.contract_rejected = 0
        self.broker_faults = 0
        self.quarantined = 0
        self.rej_etf = 0
        self.rej_adr = 0
        self.rej_trust = 0
        self.rej_reit = 0
        self.rej_etn = 0
        self.rej_fund = 0
        self.rej_non_common = 0
        self.rej_bad_exchange = 0
        self.rej_contract_invalid = 0
        self.rej_contract_timeout = 0
        self.rej_denied_ticker = 0
        self.rej_bad_ticker_shape = 0
        self.rej_otc_exchange = 0

    def bump_static_reject(self, reason: str):
        self.static_rejected += 1
        attr = f"rej_{reason}"
        if hasattr(self, attr):
            setattr(self, attr, getattr(self, attr) + 1)

    def bump_gate_reject(self, reason: str):
        self.gate_rejected += 1
        attr = f"rej_{reason}"
        if hasattr(self, attr):
            setattr(self, attr, getattr(self, attr) + 1)

    def top_static_reject(self) -> str:
        pairs = [
            ("etf", self.rej_etf), ("adr", self.rej_adr),
            ("trust", self.rej_trust), ("reit", self.rej_reit),
            ("etn", self.rej_etn), ("fund", self.rej_fund),
            ("non_common", self.rej_non_common),
            ("bad_exchange", self.rej_bad_exchange),
            ("contract_invalid", self.rej_contract_invalid),
            ("contract_timeout", self.rej_contract_timeout),
            ("denied_ticker", self.rej_denied_ticker),
            ("bad_ticker_shape", self.rej_bad_ticker_shape),
            ("otc_exchange", self.rej_otc_exchange),
        ]
        best = max(pairs, key=lambda x: x[1])
        return best[0] if best[1] > 0 else "none"

    def log_line(self) -> str:
        vc = _verdict_counts()
        return (
            f"[FUNNEL] raw={self.scan_raw} "
            f"gate_rej={self.gate_rejected} dedup={self.deduped} "
            f"quarantined={self.quarantined} "
            f"static_ok={self.static_qualified} static_rej={self.static_rejected} "
            f"contract_ok={self.contract_ok} contract_rej={self.contract_rejected} "
            f"broker_faults={self.broker_faults} "
            f"verdicts(h={vc['hard_deny']}/s={vc['soft_deny']}/b={vc['broker_unstable']}/v={vc['validated']}) "
            f"top_reject={self.top_static_reject()}"
        )


_funnel = _FunnelCounters()


# ── Scanner source quality tracker ──────────────────────────────────
class _SourceTracker:
    """Per-scan-source counters: how many symbols each source yields vs rejects."""

    def __init__(self):
        self._seen: dict[str, int] = {}
        self._rejected: dict[str, int] = {}
        self._accepted: dict[str, int] = {}

    def record_seen(self, source: str):
        if source:
            self._seen[source] = self._seen.get(source, 0) + 1

    def record_rejected(self, source: str):
        if source:
            self._rejected[source] = self._rejected.get(source, 0) + 1

    def record_accepted(self, source: str):
        if source:
            self._accepted[source] = self._accepted.get(source, 0) + 1

    def log_line(self) -> str:
        parts = []
        for src in sorted(self._seen):
            s = self._seen.get(src, 0)
            r = self._rejected.get(src, 0)
            a = self._accepted.get(src, 0)
            pct = int(100 * r / s) if s else 0
            parts.append(f"{src}={a}/{s}({pct}%rej)")
        return f"[SOURCE] {' '.join(parts)}" if parts else "[SOURCE] (none)"

    def reset(self):
        self._seen.clear()
        self._rejected.clear()
        self._accepted.clear()


_source_tracker = _SourceTracker()


# ── Symbol verdict cache: tiered confidence with TTLs ─────────────
# Replaces the simple quarantine with a full verdict taxonomy:
#   hard_deny      — structurally untradeable (ETF/ADR/trust/etc), long TTL
#   soft_deny      — failed qualification but might change (contract_invalid), medium TTL
#   broker_unstable — failed due to infra fault, short TTL (retry soon)
#   validated      — confirmed common stock, long TTL (skip re-qualification)
_VERDICT_TTL = {
    "hard_deny":        int(os.getenv("TL_VERDICT_TTL_HARD",   str(24 * 60 * 60))),  # 24h
    "soft_deny":        int(os.getenv("TL_VERDICT_TTL_SOFT",   str(6 * 60 * 60))),   # 6h
    "broker_unstable":  int(os.getenv("TL_VERDICT_TTL_BROKER", str(10 * 60))),        # 10min
    "validated":        int(os.getenv("TL_VERDICT_TTL_VALID",  str(12 * 60 * 60))),   # 12h
}

# Reasons that map to hard_deny (structurally permanent)
_HARD_DENY_REASONS = frozenset({
    "etf", "etn", "adr", "trust", "fund", "reit",
    "non_common", "bad_exchange",
    "denied_ticker", "bad_ticker_shape", "otc_exchange",
})
# Reasons that map to soft_deny (might change or be data-quality issue)
_SOFT_DENY_REASONS = frozenset({
    "contract_invalid",
})


class _VerdictEntry:
    __slots__ = ("tier", "reason", "expires_at")

    def __init__(self, tier: str, reason: str, expires_at: float):
        self.tier = tier
        self.reason = reason
        self.expires_at = expires_at


_verdicts: dict[str, _VerdictEntry] = {}


def _record_verdict(symbol: str, reason: str, tier: str = "") -> None:
    """Record a symbol verdict. Tier is auto-classified from reason if not given."""
    if not symbol:
        return
    if not tier:
        if reason in _HARD_DENY_REASONS:
            tier = "hard_deny"
        elif reason in _SOFT_DENY_REASONS:
            tier = "soft_deny"
        elif reason == "broker_fault" or reason.startswith("broker_"):
            tier = "broker_unstable"
        else:
            tier = "soft_deny"
    ttl = _VERDICT_TTL.get(tier, _VERDICT_TTL["soft_deny"])
    _verdicts[symbol.upper()] = _VerdictEntry(
        tier=tier, reason=reason, expires_at=time.time() + ttl,
    )


def _record_validated(symbol: str) -> None:
    """Mark symbol as validated common stock (skip future re-qualification)."""
    if not symbol:
        return
    ttl = _VERDICT_TTL["validated"]
    _verdicts[symbol.upper()] = _VerdictEntry(
        tier="validated", reason="ok", expires_at=time.time() + ttl,
    )


def _check_verdict(symbol: str) -> tuple[Optional[str], Optional[str]]:
    """Return (tier, reason) or (None, None) if no active verdict."""
    if not symbol:
        return None, None
    key = symbol.upper()
    entry = _verdicts.get(key)
    if entry is None:
        return None, None
    if entry.expires_at <= time.time():
        _verdicts.pop(key, None)
        return None, None
    return entry.tier, entry.reason


def _verdict_counts() -> dict[str, int]:
    """Return counts per tier (for heartbeat)."""
    counts: dict[str, int] = {"hard_deny": 0, "soft_deny": 0, "broker_unstable": 0, "validated": 0}
    now = time.time()
    for entry in _verdicts.values():
        if entry.expires_at > now:
            counts[entry.tier] = counts.get(entry.tier, 0) + 1
    return counts


def _fast_qualify_symbol_meta(
    symbol: str,
    sec_type: str = "",
    long_name: str = "",
    primary_exchange: str = "",
) -> tuple:
    """Fast static pre-qualification. Returns (ok, reason)."""
    st = (sec_type or "").strip().upper()
    ln = (long_name or "").strip()
    px = (primary_exchange or "").strip().upper()

    if st and st != "STK":
        return False, "non_common"

    if ln:
        for rx, reason in _RE_BAD_NAME:
            if rx.search(ln):
                return False, reason

    if px and px not in _ALLOWED_PRIMARY_EXCHANGES:
        return False, "bad_exchange"

    return True, "ok"


def _safe_qualify_contract(ib, symbol: str, valid_contracts: dict) -> tuple:
    """Qualify contract with IB and run static meta-filter on details.

    Returns (ok, contract_or_None, reason_str).
    Separates broker faults from legitimate rejections.
    """
    if symbol in valid_contracts:
        _funnel.contract_ok += 1
        return True, valid_contracts[symbol], "cached"

    # Universe gate: reject known-bad tickers before any IBKR traffic
    gate_ok, gate_reason = _universe_gate(symbol)
    if not gate_ok:
        _funnel.bump_gate_reject(gate_reason)
        _record_verdict(symbol, gate_reason)
        return False, None, f"gate:{gate_reason}"

    # Verdict cache: skip symbols with known-bad or unstable verdicts
    v_tier, v_reason = _check_verdict(symbol)
    if v_tier in ("hard_deny", "soft_deny", "broker_unstable"):
        _funnel.quarantined += 1
        _funnel.bump_static_reject(v_reason or "contract_invalid")
        return False, None, f"verdict:{v_tier}:{v_reason}"

    # Degraded-mode guard: skip non-cached qualification while broker is unstable
    if _broker_sentinel.degraded:
        _funnel.broker_faults += 1
        return False, None, "broker_degraded"

    try:
        c = _make_ib_contract(symbol)
        qualified = ib.qualifyContracts(c)
        if not qualified:
            _funnel.contract_rejected += 1
            _funnel.bump_static_reject("contract_invalid")
            _record_verdict(symbol, "contract_invalid")
            return False, None, "contract_invalid"
    except Exception as e:
        msg = str(e)
        fault_type = _broker_sentinel.record(msg)
        _funnel.broker_faults += 1
        return False, None, f"broker_fault:{fault_type}"

    # secType check
    if c.secType != "STK":
        _funnel.contract_rejected += 1
        _funnel.bump_static_reject("non_common")
        _record_verdict(symbol, "non_common")
        return False, None, f"secType={c.secType}"

    # Fetch contract details for longName / exchange filtering
    try:
        details = ib.reqContractDetails(c)
        if details:
            cd = details[0]
            ln = (cd.longName or "").strip()
            px = (getattr(cd.contract, "primaryExchange", "") or "").strip()
            ok, reason = _fast_qualify_symbol_meta(
                symbol=symbol, long_name=ln, primary_exchange=px,
            )
            if not ok:
                _funnel.contract_rejected += 1
                _funnel.bump_static_reject(reason)
                _record_verdict(symbol, reason)
                return False, None, f"longName/{reason}: {ln}"
    except Exception as e:
        msg = str(e)
        fault_type = _broker_sentinel.record(msg)
        _funnel.broker_faults += 1
        return False, None, f"broker_fault:{fault_type}"

    valid_contracts[symbol] = c
    _funnel.contract_ok += 1
    _record_validated(symbol)
    return True, c, "ok"


# ── Paper-only throughput boost controller ──────────────────────────
_PAPER_BOOST_LOOKBACK = int(os.getenv("TL_PAPER_BOOST_LOOKBACK", "6"))
_PAPER_BOOST_DURATION = int(os.getenv("TL_PAPER_BOOST_DURATION", "3"))
_PAPER_BOOST_DELTA = float(os.getenv("TL_PAPER_BOOST_DELTA", "5.0"))


class _RejectReason:
    HYPERFILTER   = "hyperfilter"
    REGIME        = "regime"
    UNIFIED_SCORE = "unified_score"
    CATALYST_LOW  = "catalyst_low"
    INVALID_SYM   = "invalid_sym"
    QUANT_FAIL    = "quant_fail"
    SIZING_FAIL   = "sizing_fail"
    NO_CANDIDATES = "no_candidates"
    OTHER         = "other"
    SESSION_GATE  = "session_gate"
    ALREADY_ACTIVE = "already_active"


class _ThroughputCounters:
    __slots__ = (
        "candidates_seen", "intents_emitted",
        "hyperfilter_skipped", "regime_blocked",
        "unified_score_skipped", "catalyst_low_skipped",
        "invalid_sym_skipped", "quant_fail_skipped",
        "sizing_fail_skipped", "session_gate_skipped",
        "already_active_skipped", "other_skipped",
        "throttled", "carry_forward",
        "regime_reject_ctx",
    )

    def __init__(self):
        self.reset()

    def reset(self, candidates: int = 0):
        self.candidates_seen = candidates
        self.intents_emitted = 0
        self.hyperfilter_skipped = 0
        self.regime_blocked = 0
        self.unified_score_skipped = 0
        self.catalyst_low_skipped = 0
        self.invalid_sym_skipped = 0
        self.quant_fail_skipped = 0
        self.sizing_fail_skipped = 0
        self.session_gate_skipped = 0
        self.already_active_skipped = 0
        self.other_skipped = 0
        self.throttled = 0
        self.carry_forward = 0
        self.regime_reject_ctx = {}

    def log_line(self) -> str:
        _sess_ok, _sess_reason = _session_allows_new_entries()
        _rth = is_regular_trading_hours()
        from config.runtime import require_live_quotes, synthetic_ok
        return (
            f"[THROUGHPUT] seen={self.candidates_seen} "
            f"hyper={self.hyperfilter_skipped} regime={self.regime_blocked} "
            f"score={self.unified_score_skipped} catalyst={self.catalyst_low_skipped} "
            f"invalid={self.invalid_sym_skipped} quant={self.quant_fail_skipped} "
            f"sizing={self.sizing_fail_skipped} session_gate={self.session_gate_skipped} "
            f"active={self.already_active_skipped} other={self.other_skipped} "
            f"throttled={self.throttled} carry={self.carry_forward} "
            f"intents={self.intents_emitted} "
            f"top_reject={_tp_best_reject()} top_class={_tp_best_class()} "
            f"session={'RTH' if _rth else 'AFTERHOURS'} "
            f"entry_enabled={int(_sess_ok)} "
            f"live_quotes_required={int(require_live_quotes)} "
            f"synthetic_ok={int(synthetic_ok)}"
        )


class _PaperBoostState:
    __slots__ = (
        "loops_since_intent", "boost_active", "loops_remaining",
        "near_miss_this_loop",
    )

    def __init__(self):
        self.loops_since_intent = 0
        self.boost_active = False
        self.loops_remaining = 0
        self.near_miss_this_loop = False


_tp = _ThroughputCounters()
_paper_boost = _PaperBoostState()


def _paper_boost_effective_min(base_min: float) -> float:
    """Return effective min score with boost applied. No-op when not paper."""
    if not is_paper or not _paper_boost.boost_active:
        return base_min
    return max(base_min * 0.5, base_min - _PAPER_BOOST_DELTA)


def _tp_best_reject() -> str:
    """Return the rejection reason with the highest count this cycle."""
    pairs = [
        (_RejectReason.HYPERFILTER, _tp.hyperfilter_skipped),
        (_RejectReason.REGIME, _tp.regime_blocked),
        (_RejectReason.UNIFIED_SCORE, _tp.unified_score_skipped),
        (_RejectReason.CATALYST_LOW, _tp.catalyst_low_skipped),
        (_RejectReason.INVALID_SYM, _tp.invalid_sym_skipped),
        (_RejectReason.QUANT_FAIL, _tp.quant_fail_skipped),
        (_RejectReason.SIZING_FAIL, _tp.sizing_fail_skipped),
        (_RejectReason.SESSION_GATE, _tp.session_gate_skipped),
        (_RejectReason.ALREADY_ACTIVE, _tp.already_active_skipped),
        (_RejectReason.OTHER, _tp.other_skipped),
        ("throttled", _tp.throttled),
    ]
    best = max(pairs, key=lambda x: x[1])
    return best[0] if best[1] > 0 else _RejectReason.NO_CANDIDATES


# ── Reject classification (strategy / data / operational) ────────────

_REJECT_CLASS = {
    _RejectReason.HYPERFILTER:    "strategy",
    _RejectReason.REGIME:         "strategy",
    _RejectReason.UNIFIED_SCORE:  "strategy",
    _RejectReason.CATALYST_LOW:   "strategy",
    _RejectReason.INVALID_SYM:    "data",
    _RejectReason.QUANT_FAIL:     "data",
    _RejectReason.SIZING_FAIL:    "data",
    _RejectReason.SESSION_GATE:   "operational",
    _RejectReason.ALREADY_ACTIVE: "operational",
    _RejectReason.OTHER:          "operational",
    "throttled":                  "operational",
}


def _tp_best_class() -> str:
    """Return the reject class (strategy/data/operational) with the most rejects."""
    class_totals = {"strategy": 0, "data": 0, "operational": 0}
    class_totals["strategy"] = (
        _tp.hyperfilter_skipped + _tp.regime_blocked
        + _tp.unified_score_skipped + _tp.catalyst_low_skipped
    )
    class_totals["data"] = (
        _tp.invalid_sym_skipped + _tp.quant_fail_skipped
        + _tp.sizing_fail_skipped
    )
    class_totals["operational"] = (
        _tp.session_gate_skipped + _tp.already_active_skipped
        + _tp.other_skipped + _tp.throttled
    )
    best = max(class_totals.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "none"


# ── Session executability check (pre-risk gate) ──────────────────────

def _session_allows_new_entries() -> tuple:
    """Return (allowed: bool, reason: str) for new entry intents.

    Checks market hours and AH configuration so the signal arm
    skips emitting intents that the risk arm would certainly reject.
    Manages exits are unaffected.
    """
    rth = is_regular_trading_hours()
    if rth:
        return True, "rth"
    from config.runtime import allow_extended, ah_entry_enabled, require_live_quotes, synthetic_ok
    if not allow_extended:
        return False, "outside_market_hours"
    if not ah_entry_enabled:
        return False, "ah_entry_disabled"
    if require_live_quotes and not synthetic_ok:
        return False, "require_live_quotes_no_synthetic"
    return True, "extended_hours_enabled"


def _ledger_log_line() -> str:
    """Full-pipeline conservation ledger: every candidate accounted once.

    raw = gate_rej + dedup + quarantined + static_rej + contract_rej
          + broker_faults + (regime + hyper + score + catalyst_low
          + invalid + quant + sizing + session_gate + active
          + other + throttled + carry)
          + intents
    """
    upstream = (
        _funnel.gate_rejected + _funnel.deduped + _funnel.quarantined
        + _funnel.static_rejected + _funnel.contract_rejected
        + _funnel.broker_faults
    )
    downstream = (
        _tp.regime_blocked + _tp.hyperfilter_skipped
        + _tp.unified_score_skipped + _tp.catalyst_low_skipped
        + _tp.invalid_sym_skipped + _tp.quant_fail_skipped
        + _tp.sizing_fail_skipped + _tp.session_gate_skipped
        + _tp.already_active_skipped + _tp.other_skipped
        + _tp.throttled + _tp.carry_forward
    )
    accounted = upstream + downstream + _tp.intents_emitted
    leak = _funnel.scan_raw - accounted if _funnel.scan_raw > 0 else 0
    return (
        f"[LEDGER] raw={_funnel.scan_raw} "
        f"gate={_funnel.gate_rejected} dedup={_funnel.deduped} "
        f"quar={_funnel.quarantined} "
        f"static={_funnel.static_rejected} contract={_funnel.contract_rejected} "
        f"broker={_funnel.broker_faults} "
        f"regime={_tp.regime_blocked} hyper={_tp.hyperfilter_skipped} "
        f"score={_tp.unified_score_skipped} "
        f"sizing={_tp.sizing_fail_skipped} session_gate={_tp.session_gate_skipped} "
        f"active={_tp.already_active_skipped} "
        f"throttled={_tp.throttled} carry={_tp.carry_forward} "
        f"intents={_tp.intents_emitted} "
        f"leak={leak}"
    )


def _tp_reconcile(regime_str: str) -> None:
    """Reconcile candidate-loop funnel: sum(skip buckets) + intents must equal seen.

    Logs ERROR on mismatch, prints regime-context summary for CHOP/PANIC.
    """
    handled = (
        _tp.invalid_sym_skipped + _tp.already_active_skipped
        + _tp.hyperfilter_skipped + _tp.regime_blocked
        + _tp.unified_score_skipped + _tp.catalyst_low_skipped
        + _tp.quant_fail_skipped + _tp.sizing_fail_skipped
        + _tp.session_gate_skipped + _tp.other_skipped
        + _tp.throttled + _tp.carry_forward
        + _tp.intents_emitted
    )
    if handled != _tp.candidates_seen:
        delta = _tp.candidates_seen - handled
        log.error(
            "[FUNNEL_MISMATCH] handled=%d seen=%d delta=%d "
            "invalid=%d active=%d hyper=%d regime=%d score=%d cat=%d "
            "quant=%d sizing=%d session_gate=%d other=%d "
            "throttled=%d carry=%d intents=%d",
            handled, _tp.candidates_seen, delta,
            _tp.invalid_sym_skipped, _tp.already_active_skipped,
            _tp.hyperfilter_skipped, _tp.regime_blocked,
            _tp.unified_score_skipped, _tp.catalyst_low_skipped,
            _tp.quant_fail_skipped, _tp.sizing_fail_skipped,
            _tp.session_gate_skipped, _tp.other_skipped,
            _tp.throttled, _tp.carry_forward, _tp.intents_emitted,
        )
        print(
            f"[ERROR] FUNNEL_MISMATCH handled={handled} seen={_tp.candidates_seen} delta={delta}"
        )

    # ── Regime diagnostics (Task 6) ──────────────────────────────
    if regime_str in ("CHOP", "CHOPRANGE", "PANIC", "RED"):
        _strategy_rej = (
            _tp.hyperfilter_skipped + _tp.unified_score_skipped
            + _tp.catalyst_low_skipped + _tp.regime_blocked
        )
        if _strategy_rej > 0 or _tp.regime_reject_ctx:
            ctx_items = sorted(
                _tp.regime_reject_ctx.items(), key=lambda x: -x[1]
            )
            top_ctx = ctx_items[0] if ctx_items else ("none", 0)
            print(
                f"[REGIME_DIAG] regime={regime_str} "
                f"regime_rejects={_strategy_rej} "
                f"top_regime_context={top_ctx[0]}({top_ctx[1]}) "
                f"breakdown: hyper={_tp.hyperfilter_skipped} "
                f"score={_tp.unified_score_skipped} "
                f"cat={_tp.catalyst_low_skipped} "
                f"regime_gate={_tp.regime_blocked} "
                f"session_gate={_tp.session_gate_skipped}"
            )


def _paper_boost_tick() -> None:
    """Called once per evaluation cycle in paper mode. Updates boost state."""
    st = _paper_boost

    if _tp.intents_emitted > 0:
        st.loops_since_intent = 0
    else:
        st.loops_since_intent += 1

    # Decrement active boost
    if st.boost_active:
        st.loops_remaining -= 1
        if st.loops_remaining <= 0:
            st.boost_active = False
            st.loops_remaining = 0

    # Check activation condition
    if (not st.boost_active
            and st.loops_since_intent >= _PAPER_BOOST_LOOKBACK
            and st.near_miss_this_loop):
        st.boost_active = True
        st.loops_remaining = _PAPER_BOOST_DURATION
        print(
            f"PAPER_THROUGHPUT_BOOST applied: base_min={BASE_MIN_UNIFIED_SCORE:.0f} "
            f"eff_min={BASE_MIN_UNIFIED_SCORE - _PAPER_BOOST_DELTA:.0f} "
            f"loops={_PAPER_BOOST_DURATION} no_intents_for={st.loops_since_intent}"
        )

    # Status log every cycle
    print(
        f"PAPER_THROUGHPUT status: active={'1' if st.boost_active else '0'} "
        f"loops_left={st.loops_remaining} "
        f"base_min={BASE_MIN_UNIFIED_SCORE:.0f} "
        f"eff_min={_paper_boost_effective_min(BASE_MIN_UNIFIED_SCORE):.0f} "
        f"top_reject={_tp_best_reject()}"
    )


# ── Session Event Tracker ─────────────────────────────────────────

class SessionTracker:
    """Accumulates structured events for the session report and telemetry."""

    def __init__(self):
        self.start_ts = time.time()
        self.start_utc = datetime.now(timezone.utc).isoformat()
        self.symbols_scanned: Set[str] = set()
        self.signals: list = []          # {symbol, unified_score, cat_score, quant_score, gate}
        self.intents: list = []           # {symbol, qty, entry, stop, trail_activate, risk_pct}
        self.risk_approved: list = []     # {symbol, qty, entry, stop, ...}
        self.risk_rejected: list = []     # {symbol, reason}
        self.orders_placed: list = []     # {symbol, qty, entry, stop, ok, message, order_id}
        self.positions_opened: list = []  # {symbol, qty, entry, ts}
        self.positions_closed: list = []  # {symbol, pnl, close_reason, ts}
        self.trail_activations: list = [] # {symbol, trail_amt, ts}
        self.kill_switch_events: list = []
        self.errors: list = []
        self.start_equity: float = 0.0
        self.end_equity: float = 0.0
        self.regime_log: list = []        # {regime, ts}

        # ── Telemetry counters ───────────────────────────────────
        self.candidates_checked: int = 0
        self.hyper_rejections: list = []   # {symbol, reason}
        self.bounce_rejections: list = []  # {symbol, reason}
        self.score_rejections: list = []   # {symbol, unified, required}
        self.orders_cancelled: int = 0
        self.degraded_brackets: int = 0

        # ── High-water marks (updated each loop) ────────────────
        self.max_open_risk_total: float = 0.0
        self.max_open_risk_filled: float = 0.0
        self.max_open_risk_pending: float = 0.0
        self.max_concurrent_positions: int = 0
        self.max_concurrent_working: int = 0

        # ── PnL snapshots ───────────────────────────────────────
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0

    def log_event(self, tag: str, **kwargs):
        """Log a structured event to console and accumulate for report."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        detail = " ".join(f"{k}={v}" for k, v in kwargs.items())
        print(f"[{tag}] {ts} {detail}")

    def update_risk_watermarks(self, total: float, filled: float, pending: float,
                               n_positions: int, n_working: int):
        """Track peak risk and concurrency metrics."""
        if total > self.max_open_risk_total:
            self.max_open_risk_total = total
        if filled > self.max_open_risk_filled:
            self.max_open_risk_filled = filled
        if pending > self.max_open_risk_pending:
            self.max_open_risk_pending = pending
        if n_positions > self.max_concurrent_positions:
            self.max_concurrent_positions = n_positions
        if n_working > self.max_concurrent_working:
            self.max_concurrent_working = n_working

    def to_report(self) -> dict:
        elapsed = time.time() - self.start_ts
        pnl_per_trade = []
        for o in self.orders_placed:
            if o.get("ok"):
                sym = o["symbol"]
                closed = [c for c in self.positions_closed if c["symbol"] == sym]
                if closed:
                    pnl_per_trade.append({"symbol": sym, "pnl": closed[-1].get("pnl", 0.0)})
        total_pnl = sum(t.get("pnl", 0.0) for t in pnl_per_trade)
        return {
            "session_start": self.start_utc,
            "session_end": datetime.now(timezone.utc).isoformat(),
            "duration_minutes": round(elapsed / 60, 1),
            "start_equity": self.start_equity,
            "end_equity": self.end_equity,
            "symbols_scanned": len(self.symbols_scanned),
            "symbols_scanned_list": sorted(self.symbols_scanned),
            "signals_generated": len(self.signals),
            "signals": self.signals,
            "trade_intents_created": len(self.intents),
            "intents": self.intents,
            "risk_approved": len(self.risk_approved),
            "risk_rejected": len(self.risk_rejected),
            "risk_violations": self.risk_rejected,
            "orders_attempted": len(self.orders_placed),
            "orders_filled": sum(1 for o in self.orders_placed if o.get("ok")),
            "orders": self.orders_placed,
            "positions_opened": len(self.positions_opened),
            "positions_closed": len(self.positions_closed),
            "pnl_per_trade": pnl_per_trade,
            "total_pnl": total_pnl,
            "trail_activations": self.trail_activations,
            "kill_switch_events": self.kill_switch_events,
            "system_errors": self.errors,
            "regime_log": self.regime_log,
        }

    def build_telemetry(self) -> dict:
        """Build the full telemetry payload for session_telemetry.json."""
        elapsed = time.time() - self.start_ts
        report = self.to_report()

        # Score summaries
        unified_scores = [s["unified_score"] for s in self.signals]
        catalyst_scores = [s.get("catalyst_score", 0) for s in self.signals]
        quant_scores = [s.get("quant_score", 0) for s in self.signals]

        def _avg(lst):
            return round(sum(lst) / len(lst), 1) if lst else 0.0

        # Rejection breakdown: top reasons by frequency
        def _top_reasons(rejections, key="reason", top_n=10):
            counts: Dict[str, int] = {}
            for r in rejections:
                reason = r.get(key, "unknown")
                # Normalize: strip symbol-specific values for grouping
                counts[reason] = counts.get(reason, 0) + 1
            return sorted(counts.items(), key=lambda x: -x[1])[:top_n]

        orders_working = sum(1 for o in self.orders_placed
                             if o.get("ok") and o.get("status") == "WORKING")
        orders_queued = sum(1 for o in self.orders_placed
                            if o.get("ok") and o.get("status") == "QUEUED_NEXT_SESSION")

        return {
            "session": {
                "session_id": self.start_utc.replace(":", "").replace("-", "")[:15],
                "start_time": self.start_utc,
                "end_time": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(elapsed, 1),
                "mode": "PAPER" if is_paper() else "LIVE",
                "backend": execution_backend(),
                "armed": is_armed(),
            },
            "counts": {
                "symbols_scanned": len(self.symbols_scanned),
                "candidates_checked": self.candidates_checked,
                "signals_generated": len(self.signals),
                "trade_intents_created": len(self.intents),
                "risk_approved": len(self.risk_approved),
                "risk_rejected": len(self.risk_rejected),
                "orders_submitted": sum(1 for o in self.orders_placed if o.get("ok")),
                "orders_working": orders_working,
                "orders_queued_next_session": orders_queued,
                "orders_filled": len(self.positions_opened),
                "orders_cancelled": self.orders_cancelled,
                "degraded_brackets": self.degraded_brackets,
                "positions_opened": len(self.positions_opened),
                "positions_closed": len(self.positions_closed),
                "trail_activations": len(self.trail_activations),
                "system_errors": len(self.errors),
            },
            "scores": {
                "avg_unified_score": _avg(unified_scores),
                "min_unified_score": round(min(unified_scores), 1) if unified_scores else 0.0,
                "max_unified_score": round(max(unified_scores), 1) if unified_scores else 0.0,
                "avg_catalyst_score": _avg(catalyst_scores),
                "avg_quant_score": _avg(quant_scores),
            },
            "risk_pnl": {
                "start_equity": self.start_equity,
                "end_equity": self.end_equity,
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(self.unrealized_pnl, 2),
                "total_pnl": round(self.realized_pnl + self.unrealized_pnl, 2),
                "max_open_risk_total": round(self.max_open_risk_total, 4),
                "max_open_risk_filled": round(self.max_open_risk_filled, 4),
                "max_open_risk_pending": round(self.max_open_risk_pending, 4),
                "max_concurrent_positions": self.max_concurrent_positions,
                "max_concurrent_working_orders": self.max_concurrent_working,
            },
            "rejections": {
                "hyper_filter": {
                    "total": len(self.hyper_rejections),
                    "top_reasons": _top_reasons(self.hyper_rejections),
                },
                "bounce_filter": {
                    "total": len(self.bounce_rejections),
                    "top_reasons": _top_reasons(self.bounce_rejections),
                },
                "score_filter": {
                    "total": len(self.score_rejections),
                    "top_reasons": _top_reasons(self.score_rejections),
                },
                "risk_filter": {
                    "total": len(self.risk_rejected),
                    "top_reasons": _top_reasons(self.risk_rejected),
                },
            },
            "detail": {
                "signals": self.signals,
                "intents": self.intents,
                "orders": self.orders_placed,
                "positions_opened": self.positions_opened,
                "positions_closed": self.positions_closed,
                "trail_activations": self.trail_activations,
                "kill_switch_events": self.kill_switch_events,
                "errors": self.errors,
                "regime_log": self.regime_log,
            },
        }

    def print_summary(self):
        """Print a human-readable session summary to console."""
        t = self.build_telemetry()
        c = t["counts"]
        s = t["scores"]
        r = t["risk_pnl"]
        rej = t["rejections"]
        sess = t["session"]

        dur_min = round(t["session"]["duration_seconds"] / 60, 1)

        # Collect top 5 rejection reasons across all filters
        all_reasons = []
        for filt in ("hyper_filter", "bounce_filter", "score_filter", "risk_filter"):
            for reason, count in rej[filt]["top_reasons"]:
                all_reasons.append((f"{filt}: {reason}", count))
        all_reasons.sort(key=lambda x: -x[1])
        top5 = all_reasons[:5]

        print("\n" + "=" * 64)
        print("  SESSION TELEMETRY SUMMARY")
        print("=" * 64)
        print(f"  session_id     : {sess['session_id']}")
        print(f"  duration       : {dur_min} min")
        print(f"  mode           : {sess['mode']}  backend={sess['backend']}  armed={sess['armed']}")
        print("-" * 64)
        print("  PIPELINE")
        print(f"    scanned          : {c['symbols_scanned']}")
        print(f"    candidates       : {c['candidates_checked']}")
        print(f"    signals          : {c['signals_generated']}")
        print(f"    intents          : {c['trade_intents_created']}")
        print(f"    risk_approved    : {c['risk_approved']}")
        print(f"    risk_rejected    : {c['risk_rejected']}")
        print(f"    orders_submitted : {c['orders_submitted']}")
        print(f"    orders_working   : {c['orders_working']}")
        print(f"    orders_queued    : {c['orders_queued_next_session']}")
        print(f"    orders_filled    : {c['orders_filled']}")
        print(f"    orders_cancelled : {c['orders_cancelled']}")
        print(f"    degraded_brackets: {c['degraded_brackets']}")
        print(f"    positions_opened : {c['positions_opened']}")
        print(f"    positions_closed : {c['positions_closed']}")
        print(f"    trail_activations: {c['trail_activations']}")
        print(f"    system_errors    : {c['system_errors']}")
        print("-" * 64)
        print("  SCORES")
        print(f"    unified  : avg={s['avg_unified_score']}  min={s['min_unified_score']}  max={s['max_unified_score']}")
        print(f"    catalyst : avg={s['avg_catalyst_score']}")
        print(f"    quant    : avg={s['avg_quant_score']}")
        print("-" * 64)
        print("  RISK / PnL")
        print(f"    equity           : ${r['start_equity']:,.2f} -> ${r['end_equity']:,.2f}")
        print(f"    realized_pnl     : ${r['realized_pnl']:,.2f}")
        print(f"    unrealized_pnl   : ${r['unrealized_pnl']:,.2f}")
        print(f"    total_pnl        : ${r['total_pnl']:,.2f}")
        print(f"    max_risk_total   : {r['max_open_risk_total']:.4f}")
        print(f"    max_risk_filled  : {r['max_open_risk_filled']:.4f}")
        print(f"    max_risk_pending : {r['max_open_risk_pending']:.4f}")
        print(f"    max_positions    : {r['max_concurrent_positions']}")
        print(f"    max_working      : {r['max_concurrent_working_orders']}")
        print("-" * 64)
        print("  REJECTIONS")
        print(f"    hyper_filter  : {rej['hyper_filter']['total']}")
        print(f"    bounce_filter : {rej['bounce_filter']['total']}")
        print(f"    score_filter  : {rej['score_filter']['total']}")
        print(f"    risk_filter   : {rej['risk_filter']['total']}")
        if top5:
            print("  TOP 5 REJECTION REASONS")
            for reason, count in top5:
                print(f"    [{count:>3}x] {reason}")
        print("=" * 64)


_ENABLE_SESSION_REPORT = os.getenv("TL_SESSION_REPORT", "0") in ("1", "true", "yes")

# Session-level invalid symbol cache (to suppress repeated IB errors)
INVALID_SYMBOL_CACHE: Set[str] = set()

# Delayed trailing state tracking
TRAIL_STATE: Dict[str, str] = {}  # symbol -> "pending" | "activated"
TRAIL_LOGGED: Set[str] = set()  # session-level dedup for activation logs

# Symbol-level order lock: prevent duplicate entries
SYMBOL_COOLDOWN_SECONDS = int(os.getenv("TRADE_LABS_SYMBOL_COOLDOWN_SECONDS", "900"))
symbol_lock_ts: Dict[str, float] = {}  # symbol -> timestamp of last bracket attempt


def is_symbol_locked(ib: IB, sym: str, symbol_lock_ts: Dict[str, float], now: float, cooldown: int) -> Tuple[bool, str]:
    """
    Check if a symbol is locked (can't place new bracket).
    
    Returns (locked: bool, reason: str):
    - (True, "position_open") if symbol has non-zero position
    - (True, "open_trade_status=...") if symbol in openTrades with non-terminal status
    - (True, "trail_state_present") if symbol in TRAIL_STATE dict
    - (True, "cooldown_active(...s)") if recent attempt within cooldown window
    - (False, "unlocked") if available
    """
    # Check 1: Active position
    try:
        for pos in ib.positions():
            if pos.contract.symbol == sym and pos.position != 0:
                return True, "position_open"
    except Exception as e:
        pass  # Silent fail on IB error
    
    # Check 2: Open trades (check status)
    try:
        for trade in ib.openTrades():
            if trade.contract.symbol == sym:
                st = getattr(trade.orderStatus, "status", "")
                if st not in ("Filled", "Cancelled", "Inactive"):
                    return True, f"open_trade_status={st}"
    except Exception as e:
        pass  # Silent fail
    
    # Check 3: Trail state (symbol already has pending/active trail)
    if sym in TRAIL_STATE:
        return True, "trail_state_present"
    
    # Check 4: Recent bracket attempt cooldown
    last = symbol_lock_ts.get(sym)
    if last and (now - last) < cooldown:
        remaining = int(cooldown - (now - last))
        return True, f"cooldown_active({remaining}s)"
    
    return False, "unlocked"


def connect_ib() -> IB:
    ib = IB()
    ib.RequestTimeout = 5

    fallback_span = max(1, int(os.getenv("TRADE_LABS_IB_CLIENT_ID_SPAN", "10")))
    last_error: Optional[Exception] = None
    connected_client_id: Optional[int] = None

    for offset in range(fallback_span):
        client_id = IB_CLIENT_ID + offset
        try:
            ib.connect(IB_HOST, IB_PORT, clientId=client_id, timeout=10)
            connected_client_id = client_id
            break
        except Exception as e:
            last_error = e
            msg = str(e)
            if "already in use" in msg.lower() or "326" in msg:
                print(f"[IB] clientId {client_id} busy, trying next...")
            else:
                print(f"[IB] connect failed with clientId {client_id}: {e}")
            try:
                ib.disconnect()
            except Exception:
                pass

    if connected_client_id is None:
        if last_error is not None:
            raise last_error
        raise ConnectionError("Unable to connect to IB: no available clientId")

    if connected_client_id != IB_CLIENT_ID:
        print(f"[IB] Connected using fallback clientId={connected_client_id} (base={IB_CLIENT_ID})")

    orig_error = ib.wrapper.error
    def quiet_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (1100, 1101, 1102):
            st = scanner_session_state()
            pending_scanner_ids = list(st.pending_scanners.keys())
            for pending_req_id in pending_scanner_ids:
                try:
                    ib.client.cancelScannerSubscription(pending_req_id)
                except Exception:
                    pass
            scanner_reset_generation(f"ib_error_{errorCode}")
            scanner_mark_restart(f"ib_error_{errorCode}")
            setattr(ib, "_scanner_restart_required", True)
            print(f"[SCANNER] connectivity_event code={errorCode} reqId={reqId} restart_scheduled=1")
        if errorCode in (162, 10089, 10168):
            return
        if errorCode == 200 and "No security definition" in errorString:
            return
        return orig_error(reqId, errorCode, errorString, advancedOrderRejectJson)
    ib.wrapper.error = quiet_error

    orig_start_req = ib.wrapper.startReq
    def safe_start_req(reqId, *args, **kwargs):
        result = orig_start_req(reqId, *args, **kwargs)
        if isinstance(reqId, int) and reqId > 0:
            scanner_note_request(reqId, "contract_details")
        return result
    ib.wrapper.startReq = safe_start_req

    orig_contract_details = ib.wrapper.contractDetails
    def safe_contract_details(reqId, contractDetails):
        if not scanner_session_state().pending_contract_details.get(reqId):
            scanner_note_stale_callback_ignored("contractDetails", reqId)
            return
        if not scanner_session_state().pending_contract_details[reqId].generation_id == scanner_session_state().generation_id:
            scanner_note_stale_callback_ignored("contractDetails", reqId)
            return
        try:
            return orig_contract_details(reqId, contractDetails)
        except KeyError:
            scanner_note_stale_callback_ignored("contractDetails", reqId)
            return
    ib.wrapper.contractDetails = safe_contract_details

    orig_contract_details_end = ib.wrapper.contractDetailsEnd
    def safe_contract_details_end(reqId):
        if not scanner_session_state().pending_contract_details.get(reqId):
            scanner_note_stale_callback_ignored("contractDetailsEnd", reqId)
            return
        if not scanner_session_state().pending_contract_details[reqId].generation_id == scanner_session_state().generation_id:
            scanner_note_stale_callback_ignored("contractDetailsEnd", reqId)
            scanner_note_request_end(reqId)
            return
        scanner_note_request_end(reqId)
        try:
            return orig_contract_details_end(reqId)
        except KeyError:
            scanner_note_stale_callback_ignored("contractDetailsEnd", reqId)
            return
    ib.wrapper.contractDetailsEnd = safe_contract_details_end

    orig_scanner_data = ib.wrapper.scannerData
    def safe_scanner_data(reqId, rank, contractDetails, distance, benchmark, projection, legsStr):
        if reqId not in scanner_session_state().pending_scanners:
            scanner_note_request(reqId, "scanner")
        if not scanner_session_state().pending_scanners.get(reqId):
            scanner_note_stale_callback_ignored("scannerData", reqId)
            return
        if not scanner_session_state().pending_scanners[reqId].generation_id == scanner_session_state().generation_id:
            scanner_note_stale_callback_ignored("scannerData", reqId)
            return
        try:
            return orig_scanner_data(reqId, rank, contractDetails, distance, benchmark, projection, legsStr)
        except KeyError:
            scanner_note_stale_callback_ignored("scannerData", reqId)
            return
    ib.wrapper.scannerData = safe_scanner_data

    orig_scanner_data_end = ib.wrapper.scannerDataEnd
    def safe_scanner_data_end(reqId):
        if not scanner_session_state().pending_scanners.get(reqId):
            scanner_note_stale_callback_ignored("scannerDataEnd", reqId)
            return
        if not scanner_session_state().pending_scanners[reqId].generation_id == scanner_session_state().generation_id:
            scanner_note_stale_callback_ignored("scannerDataEnd", reqId)
            scanner_note_request_end(reqId)
            return
        scanner_note_request_end(reqId)
        try:
            return orig_scanner_data_end(reqId)
        except KeyError:
            scanner_note_stale_callback_ignored("scannerDataEnd", reqId)
            return
    ib.wrapper.scannerDataEnd = safe_scanner_data_end

    return ib


def _reset_scanner_generation(ib: IB, reason: str) -> None:
    st = scanner_session_state()
    pending_scanner_ids = list(st.pending_scanners.keys())
    for pending_req_id in pending_scanner_ids:
        try:
            ib.client.cancelScannerSubscription(pending_req_id)
        except Exception:
            pass
    scanner_reset_generation(reason)
    scanner_mark_restart(reason)
    setattr(ib, "_scanner_restart_required", True)


def is_valid_stock_contract(
    ib: IB,
    symbol: str,
    valid_contracts: Optional[Dict[str, Stock]] = None,
) -> Tuple[bool, str]:
    """
    Validate that symbol is a tradeable stock (not ETF/ETN/etc).
    Uses _safe_qualify_contract + _fast_qualify_symbol_meta for
    consistent upstream filtering with funnel accounting.

    Returns:
        (is_valid, reason_if_invalid)
    """
    # Blocklist takes precedence
    if symbol in STOCK_BLOCKLIST:
        return False, "In blocklist"

    if not coarse_symbol_allowed(symbol):
        return False, "coarse_symbol_hygiene"

    # Allowlist always passes
    if symbol in STOCK_ALLOWLIST:
        return True, ""

    contracts = valid_contracts if valid_contracts is not None else {}
    ok, c, reason = _safe_qualify_contract(ib, symbol, contracts)
    if valid_contracts is not None and c is not None:
        valid_contracts[symbol] = c
    if not ok:
        return False, reason
    return True, ""


def _contract(symbol: str) -> Stock:
    return _make_ib_contract(symbol)


def get_recent_price_1m(ib: IB, symbol: str) -> float:
    c = _contract(symbol)
    ib.qualifyContracts(c)
    try:
        bars = ib.reqHistoricalData(
            c, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=False, formatDate=1
        )
        df = util.df(bars)
        if df is not None and not df.empty and "close" in df.columns:
            px = float(df["close"].iloc[-1])
            if math.isfinite(px) and px > 0:
                return px
    except Exception:
        pass

    try:
        df_d = get_daily_30d(ib, symbol)
        if df_d is not None and not df_d.empty and "close" in df_d.columns:
            px = float(df_d["close"].iloc[-1])
            if math.isfinite(px) and px > 0:
                return px
    except Exception:
        pass

    snap = get_last_price_snapshot(ib, c)
    if snap is not None:
        return float(snap)
    raise RuntimeError("no price data")


def get_daily_30d(ib: IB, symbol: str):
    c = _contract(symbol)
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(
        c, endDateTime="", durationStr="30 D", barSizeSetting="1 day",
        whatToShow="TRADES", useRTH=True, formatDate=1
    )
    return util.df(bars)


def atr14_from_daily(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = tr1.combine(tr2, max).combine(tr3, max)

    atr = tr.rolling(14).mean().dropna()
    return float(atr.iloc[-1])


def get_equity(ib: IB) -> float:
    for v in ib.accountValues():
        if v.tag == "NetLiquidation":
            return float(v.value)
    return 0.0


def get_active_symbols(ib: IB) -> Set[str]:
    syms = set()
    for tr in ib.openTrades():
        syms.add(tr.contract.symbol)
    for p in ib.positions():
        if p.position != 0:
            syms.add(p.contract.symbol)
    return syms


def get_filled_symbols(ib: IB) -> Set[str]:
    """Return symbols that have actual filled positions (non-zero shares)."""
    return {p.contract.symbol for p in ib.positions() if p.position != 0}


def get_working_order_symbols(ib: IB) -> Set[str]:
    """Return symbols with submitted-but-unfilled parent orders."""
    filled = get_filled_symbols(ib)
    working = set()
    for trade in ib.openTrades():
        sym = trade.contract.symbol
        if sym in filled:
            continue
        st = getattr(trade.orderStatus, "status", "")
        if st not in ("Filled", "Cancelled", "Inactive"):
            working.add(sym)
    return working


def is_regular_trading_hours() -> bool:
    """Check if current US Eastern time is within regular trading hours (9:30-16:00)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    t = et.time()
    from datetime import time as dt_time
    return dt_time(9, 30) <= t <= dt_time(16, 0) and et.weekday() < 5


def compute_open_risk_pct(ib: IB, equity: float, atr_cache: Dict[str, float]) -> Tuple[float, float, float]:
    """
    Open risk % = sum(shares * ATR * STOP_LOSS_R) / equity

    Returns (total_risk_pct, filled_risk_pct, pending_risk_pct).

    Includes:
    - Filled positions (ib.positions) → filled_risk
    - Pending limit orders (openTrades) → pending_risk (reserved)
    
    This prevents over-stacking orders before fills in autonomous mode.
    Uses ATR cache; if ATR missing, counts 0 risk for that symbol (conservative).
    """
    if equity <= 0:
        return 0.0, 0.0, 0.0

    filled_risk_usd = 0.0
    pending_risk_usd = 0.0
    
    # Count filled positions
    for p in ib.positions():
        if p.position == 0:
            continue
        sym = p.contract.symbol
        shares = abs(float(p.position))
        atr = atr_cache.get(sym, 0.0)
        filled_risk_usd += shares * (atr * STOP_LOSS_R)
    
    # Count pending limit orders (reserved risk)
    filled_symbols = {p.contract.symbol for p in ib.positions() if p.position != 0}
    for trade in ib.openTrades():
        # Only count parent BUY/SELL limit orders (not stops/trails)
        if trade.order.orderType != "LMT":
            continue
        if trade.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
            continue
        
        sym = trade.contract.symbol
        # Skip if already counted as filled position
        if sym in filled_symbols:
            continue
        
        shares = abs(float(trade.order.totalQuantity))
        atr = atr_cache.get(sym, 0.0)
        pending_risk_usd += shares * (atr * STOP_LOSS_R)

    total = (filled_risk_usd + pending_risk_usd) / equity
    filled = filled_risk_usd / equity
    pending = pending_risk_usd / equity
    return total, filled, pending


def get_market_breadth_pct() -> Optional[float]:
    """
    Approximate market breadth using Yahoo Finance day gainers/losers counts.
    Returns a ratio in [0, 1] or None on failure.
    """
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"

    def fetch_total(scr_id: str) -> Optional[int]:
        try:
            resp = requests.get(url, params={"scrIds": scr_id, "count": 0, "start": 0}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("finance", {}).get("result", [])
            if not result:
                return None
            return int(result[0].get("total") or 0)
        except Exception:
            return None

    gainers = fetch_total("day_gainers")
    losers = fetch_total("day_losers")
    if gainers is None or losers is None:
        return None
    total = gainers + losers
    if total <= 0:
        return None
    return gainers / total


def get_scan_breadth_pct(ib: IB, scan_results: List) -> Optional[float]:
    if not scan_results:
        return None

    adv = 0
    dec = 0

    for r in scan_results[:BREADTH_SCAN_LIMIT]:
        sym = getattr(r, "symbol", None)
        if not sym:
            continue
        try:
            df_d = get_daily_30d(ib, sym)
            if df_d is None or df_d.empty or len(df_d) < 2:
                continue
            last_close = float(df_d["close"].iloc[-1])
            prev_close = float(df_d["close"].iloc[-2])
            if last_close > prev_close:
                adv += 1
            elif last_close < prev_close:
                dec += 1
        except Exception:
            continue

    total = adv + dec
    if total <= 0:
        return None
    return adv / total


def load_closed_trades() -> List[Dict[str, float]]:
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE) as f:
            trades = json.load(f)
        return [t for t in trades if t.get("status") == "CLOSED" and t.get("pnl") is not None]
    except Exception:
        return []


def cancel_order_by_id(ib: IB, order_id: int) -> bool:
    if not order_id:
        return False
    for trade in ib.openTrades():
        if trade.order and trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            return True
    return False


def get_last_price_snapshot(ib: IB, contract: Stock) -> Optional[float]:
    try:
        market_data = ib.reqMktData(contract, "", True, False)
        ib.sleep(0.1)
        if market_data.last is not None and market_data.last > 0:
            return float(market_data.last)
        if market_data.bid is not None and market_data.ask is not None:
            return float((market_data.bid + market_data.ask) / 2.0)
    except Exception:
        return None
    return None


def close_positions_by_weakness(ib: IB, armed: bool) -> None:
    positions = [p for p in ib.portfolio() if p.position != 0]
    positions.sort(key=lambda p: float(p.unrealizedPNL or 0.0))

    for p in positions:
        action = "SELL" if p.position > 0 else "BUY"
        qty = abs(int(p.position))
        if qty <= 0:
            continue
        if not armed:
            print(f"[KILL_SWITCH] SIM close {p.contract.symbol} qty={qty}")
            continue
        order = MarketOrder(action, qty)
        ib.placeOrder(p.contract, order)
        ib.sleep(0.1)


def main():
    tracker = SessionTracker()
    _sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dist_analyzer = SignalDistributionAnalyzer(
        session_id=_sid,
        unified_threshold=BASE_MIN_UNIFIED_SCORE,
        vol_accel_threshold=MIN_VOLUME_ACCEL,
        atr_pct_threshold=MIN_ATR_PCT,
        rs_threshold=MIN_RS_VS_SPY,
    )
    lifecycle = LifecycleLogger(session_id=_sid)
    journal = TradeJournal(session_id=_sid)
    dashboard = DashboardSnapshot(
        session_id=_sid,
        mode="PAPER" if is_paper() else "LIVE",
        backend=execution_backend(),
    )
    tracker.log_event("SESSION_START",
                      mode="PAPER" if is_paper() else "LIVE",
                      backend=execution_backend(),
                      armed=is_armed())

    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Live Loop (10s)")
    print(f"MODE={'PAPER' if is_paper() else 'LIVE'} BACKEND={execution_backend()} ARMED={is_armed()}\n")

    log.info("scoring_system_check threshold=%.1f scorer=catalyst_scorer_v2", MIN_CATALYST_SCORE)

    # ── Resolve and validate session config (single source of truth) ─
    rcfg = get_resolved_config()
    rcfg.log_startup_table()
    rcfg.assert_ah_coherence()

    print(f"[COOLDOWN] BRACKET_COOLDOWN_SECONDS={BRACKET_COOLDOWN_SECONDS}")
    if ENABLE_BOUNCE_MODE:
        print(
            "[BOUNCE_CFG] "
            f"min_unified={BOUNCE_MIN_UNIFIED_SCORE:.1f} "
            f"base(wr={BOUNCE_MIN_PLAYBOOK_WIN_RATE*100:.1f}% score={BOUNCE_MIN_PLAYBOOK_SCORE:.1f} mae<={BOUNCE_MAX_MAE_ABS*100:.1f}%) "
            f"requires_n>={BOUNCE_MIN_SAMPLE_SIZE}"
        )
    print(
        f"[SCORE_CFG] base_min={BASE_MIN_UNIFIED_SCORE:.1f} "
        f"conviction_min={CONVICTION_MIN_UNIFIED_SCORE:.1f} "
        f"bounce_min={BOUNCE_MIN_UNIFIED_SCORE:.1f}"
    )

    ib = connect_ib()

    # ====== INITIALIZE CATALYST ENGINE (PRIMARY) ======
    research_engine = None
    if CATALYST_ENGINE_AVAILABLE:
        try:
            finnhub_key = os.getenv("FINNHUB_API_KEY")
            hunter = CatalystHunter(finnhub_api_key=finnhub_key)
            scorer = CatalystScorer()
            research_engine = ResearchEngine(
                catalyst_hunter=hunter,
                catalyst_scorer=scorer,
            )
            print("✅ [CATALYST ENGINE] Initialized (PRIMARY source)")
            if getattr(hunter, "finnhub_key", None):
                print("✅ [FINNHUB] configured")
            else:
                print("⚠️  [FINNHUB] missing/placeholder key; source will be skipped")
        except Exception as e:
            print(f"⚠️  [CATALYST ENGINE] Failed to init: {e}")
            research_engine = None

    cached_scan = []
    last_scan_ts = 0.0
    candidate_pool = CandidatePool()
    scan_rotator = ScanRotator()
    last_catalyst_hunt_ts = 0.0
    last_print_ts = 0.0
    last_symbols: List[str] = []
    
    # Session tracking for daily kill switch
    session_started = False
    last_session_date = None

    atr_cache: Dict[str, float] = {}
    atr_cache_ts: Dict[str, float] = {}
    
    # Catalyst trading candidates cache
    catalyst_candidates = []
    catalyst_ranking = []

    # Invalid symbol cache (session-level)
    invalid_symbols: Set[str] = INVALID_SYMBOL_CACHE
    invalid_symbols_logged: Set[str] = set()

    # Valid contract cache (session-level)
    valid_contracts: Dict[str, Stock] = {}
    
    # Track last bracket submission per symbol (for throttling)
    last_bracket_ts: Dict[str, float] = {}  # symbol -> timestamp of last bracket
    last_bracket_attempt_ts = 0.0
    last_hyper_reject_ts: Dict[str, float] = {}  # symbol -> timestamp of most recent hyper-filter reject
    last_hyper_reject_reason: Dict[str, str] = {}  # symbol -> last hyper-filter reject reason
    stop_order_ids: Dict[str, int] = {}
    entry_atr_by_symbol: Dict[str, float] = {}
    trail_active_symbols: Set[str] = set()
    confirmed_fills: Set[str] = set()  # symbols with confirmed IB position fills
    catalyst_rotation = 0
    scanner_rotation = 0

    last_trail_check_ts = 0.0
    last_breadth_ts = 0.0
    last_breadth_pct: Optional[float] = None
    last_breadth_source = "n/a"

    last_scan_score_ts = 0.0
    last_scanner_scored = []

    last_closed_trade_count = 0
    loss_streak_penalty_remaining = 0
    session_peak_equity = 0.0

    trading_halted_for_day = False
    halted_date = None
    force_kill_triggered = False

    # ── Dashboard snapshot helper (captures whatever state is available) ──
    _dash_equity = 0.0
    _dash_regime: Optional[str] = None
    _dash_open_risk = _dash_filled_risk = _dash_pending_risk = 0.0
    _dash_n_pos = _dash_n_wrk = 0

    def _write_dashboard():
        try:
            recent = [e.to_dict() for e in lifecycle.events[-10:]]
        except Exception:
            recent = []
        try:
            _filled = get_filled_symbols(ib)
            _working = get_working_order_symbols(ib)
        except Exception:
            _filled = set()
            _working = set()
        try:
            dashboard.update(
                armed=is_armed(),
                equity=_dash_equity,
                regime=_dash_regime,
                breadth_pct=last_breadth_pct,
                open_risk_pct=_dash_open_risk,
                filled_risk_pct=_dash_filled_risk,
                pending_risk_pct=_dash_pending_risk,
                n_positions=_dash_n_pos,
                n_working_orders=_dash_n_wrk,
                filled_symbols=_filled,
                working_symbols=_working,
                trail_active_symbols=trail_active_symbols,
                confirmed_fills=confirmed_fills,
                signals_count=len(tracker.signals),
                intents_count=len(tracker.intents),
                orders_placed_count=len(tracker.orders_placed),
                risk_rejected_count=len(tracker.risk_rejected),
                errors_count=len(tracker.errors),
                recent_events=recent,
                market_open=is_regular_trading_hours(),
            )
        except Exception:
            pass

    try:
        while True:
            loop_start = time.time()
            now = time.time()
            armed = is_armed()

            if not ib.isConnected():
                print("[WARN] IB disconnected, reconnecting...")
                try:
                    _reset_scanner_generation(ib, "loop_disconnect")
                except Exception:
                    pass
                try:
                    ib.disconnect()
                except Exception:
                    pass
                ib = connect_ib()
                time.sleep(1.0)
                continue

            if getattr(ib, "_scanner_restart_required", False):
                candidate_pool.clear()
                cached_scan = []
                last_scanner_scored = []
                last_scan_ts = 0.0
                last_scan_score_ts = 0.0
                setattr(ib, "_scanner_restart_required", False)
                counters = scanner_stability_counters()
                print(
                    "[SCANNER] restart_applied "
                    f"generation={scanner_session_state().generation_id} "
                    f"stale_ignored={counters['scanner_stale_callback_ignored']} "
                    f"resets={counters['scanner_generation_resets']} "
                    f"restarts={counters['scanner_restart_count']}"
                )

            equity = get_equity(ib)
            if equity <= 0:
                print("[WARN] Equity unavailable; retrying next loop.")
                _write_dashboard()
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue
            
            # Record session start on first run or new trading day
            current_session_date = datetime.now(timezone.utc).date()
            if not session_started or last_session_date != current_session_date:
                record_session_start_equity(equity)
                session_started = True
                last_session_date = current_session_date
                print(f"[SESSION] Started with equity: ${equity:,.2f}")
                if tracker.start_equity == 0.0:
                    tracker.start_equity = equity

            _dash_equity = equity

            if session_peak_equity <= 0:
                session_peak_equity = equity
            else:
                session_peak_equity = max(session_peak_equity, equity)

            if trading_halted_for_day and halted_date != current_session_date:
                trading_halted_for_day = False
                halted_date = None

            force_kill = os.getenv("TRADE_LABS_FORCE_KILL") == "1" and not force_kill_triggered
            if force_kill or is_kill_switch_active(ib):
                if not trading_halted_for_day:
                    reason = "FORCE_KILL" if force_kill else "daily_loss_threshold"
                    tracker.kill_switch_events.append({"reason": reason, "ts": datetime.now(timezone.utc).isoformat()})
                    if force_kill:
                        print("[KILL_SWITCH] Forced trigger via TRADE_LABS_FORCE_KILL=1.")
                        force_kill_triggered = True
                        try:
                            os.environ.pop("TRADE_LABS_FORCE_KILL", None)
                        except:
                            pass
                    print("[KILL_SWITCH] Triggered. Canceling orders and flattening positions.")
                    try:
                        print("[KILL_SWITCH] Canceling all open orders...")
                        ib.reqGlobalCancel()
                    except Exception:
                        pass
                    print("[KILL_SWITCH] Flattening positions...")
                    close_positions_by_weakness(ib, armed)
                    trading_halted_for_day = True
                    halted_date = current_session_date
                    print("[KILL_SWITCH] Trading halted for the day.")
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            if trading_halted_for_day:
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            closed_trades = load_closed_trades()
            if len(closed_trades) > last_closed_trade_count:
                if len(closed_trades) >= LOSS_STREAK_LOOKBACK:
                    last_two = closed_trades[-LOSS_STREAK_LOOKBACK:]
                    if all((t.get("pnl") or 0.0) < 0 for t in last_two):
                        loss_streak_penalty_remaining = LOSS_STREAK_PENALTY_TRADES
                last_closed_trade_count = len(closed_trades)

            # ====== CATALYST HUNTING (PRIMARY - every 5 minutes) ======
            catalyst_hunt_interval = 300  # Hunt catalysts every 5 minutes
            catalyst_refreshed = False
            if research_engine and ((now - last_catalyst_hunt_ts) >= catalyst_hunt_interval or not catalyst_candidates):
                try:
                    catalyst_hunt_results = research_engine.hunt_all_sources()
                    catalyst_ranking = research_engine.scorer.rank_opportunities(
                        catalyst_hunt_results,
                        max_results=CATALYST_POOL_SIZE,
                    )
                    catalyst_candidates = [opp.symbol for opp in catalyst_ranking[:10]]
                    last_catalyst_hunt_ts = now
                    catalyst_refreshed = True
                    print(f"[CATALYST] Found {len(catalyst_candidates)} high-quality opportunities")
                except Exception as e:
                    print(f"[CATALYST] hunt error: {e}")

            # ====== SCANNER HUNTING via ScanRotator + CandidatePool ======
            if candidate_pool.size() < REFILL_THRESHOLD:
                try:
                    cached_scan = scan_rotator.next_scan(ib, limit=SCAN_LIMIT)
                    last_scan_ts = now
                    added = candidate_pool.add_many(cached_scan)
                    print(f"[SCAN] refreshed: {len(cached_scan)} scanned, {added} new into pool")
                    # Guard: if every symbol was already seen and pool is
                    # still empty/below threshold, force-reset once so we
                    # don't stall the loop.
                    if added == 0 and candidate_pool.size() < REFILL_THRESHOLD:
                        print("[POOL] refill produced 0 new symbols; resetting pool and retrying once")
                        candidate_pool.clear()
                        added = candidate_pool.add_many(cached_scan)
                        print(f"[SCAN] retry: {added} symbols into pool after reset")
                except Exception as e:
                    print(f"[SCAN] error: {e}")

            scan_batch = candidate_pool.pop_many(BATCH_SIZE)

            # ====== BLEND SOURCES: CATALYST PRIMARY + SCANNER FALLBACK ======
            # Priority: 1) Catalyst candidates, 2) Scanner results
            _funnel.reset(raw=0)  # accumulate raw from each source below
            scored = []

            # Track scanned symbols
            for s in scan_batch:
                tracker.symbols_scanned.add(s.symbol if hasattr(s, 'symbol') else str(s))
            for opp in catalyst_ranking:
                tracker.symbols_scanned.add(opp.symbol)
            
            # First: use catalyst ranking directly (already scored by catalyst scorer)
            if catalyst_ranking:
                # catalyst_ranking is already a list of CatalystScore objects sorted by score
                # Validate contracts with IB before scoring
                catalyst_contracts = []
                if len(catalyst_ranking) > 0:
                    rot = catalyst_rotation % len(catalyst_ranking)
                    ranked_view = catalyst_ranking[rot:] + catalyst_ranking[:rot]
                else:
                    ranked_view = catalyst_ranking
                
                for opp in ranked_view[:CATALYST_TOP_N]:
                    _funnel.scan_raw += 1
                    _source_tracker.record_seen("catalyst")
                    if opp.symbol in invalid_symbols:
                        _funnel.deduped += 1
                        _source_tracker.record_rejected("catalyst")
                        continue
                    # Universe gate: reject known-bad tickers pre-IBKR
                    gate_ok, gate_reason = _universe_gate(opp.symbol)
                    if not gate_ok:
                        invalid_symbols.add(opp.symbol)
                        _funnel.bump_gate_reject(gate_reason)
                        _record_verdict(opp.symbol, gate_reason)
                        _source_tracker.record_rejected("catalyst")
                        if opp.symbol not in invalid_symbols_logged:
                            print(f"  [CATALYST REJECTED] {opp.symbol}: gate:{gate_reason}")
                            invalid_symbols_logged.add(opp.symbol)
                        continue
                    if not coarse_symbol_allowed(opp.symbol):
                        invalid_symbols.add(opp.symbol)
                        _funnel.bump_gate_reject("non_common")
                        _source_tracker.record_rejected("catalyst")
                        if opp.symbol not in invalid_symbols_logged:
                            print(f"  [CATALYST REJECTED] {opp.symbol}: coarse_symbol_hygiene")
                            invalid_symbols_logged.add(opp.symbol)
                        continue
                    _funnel.static_qualified += 1
                    ok, c, cd_reason = _safe_qualify_contract(ib, opp.symbol, valid_contracts)
                    if not ok:
                        _source_tracker.record_rejected("catalyst")
                        if cd_reason != "broker_fault":
                            invalid_symbols.add(opp.symbol)
                            if opp.symbol not in invalid_symbols_logged:
                                print(f"  [CATALYST REJECTED] {opp.symbol}: {cd_reason}")
                                invalid_symbols_logged.add(opp.symbol)
                        continue
                    _source_tracker.record_accepted("catalyst")
                    c.catalyst_score = opp.combined_score
                    catalyst_contracts.append(c)
                
                scored.extend(catalyst_contracts)
                if catalyst_refreshed:
                    print(f"  [CATALYST SCORED] {len(catalyst_contracts)} candidates ready (catalyst score source)")
                if catalyst_ranking:
                    catalyst_rotation = (catalyst_rotation + 1) % len(catalyst_ranking)
            
            # Scanner supplement: score the current batch from the pool
            # Scanner scores are used for DIAGNOSTICS ONLY — they do not bypass
            # the canonical catalyst_score threshold.  Scanner candidates that
            # also appear in catalyst_ranking already have a composite_score.
            if scan_batch and (now - last_scan_score_ts) >= SCANNER_SCORE_INTERVAL_SECONDS:
                last_scanner_scored = score_scan_results(ib, scan_batch, top_n=TRADE_TOP_N)
                last_scan_score_ts = now
                print(f"[SCAN] scored {len(scan_batch)} batch -> {len(last_scanner_scored)} passed (diagnostic only)")

            scanner_scored = last_scanner_scored
            if scanner_scored:
                rot = scanner_rotation % len(scanner_scored)
                scanner_ranked_view = scanner_scored[rot:] + scanner_scored[:rot]
                scanner_rotation = (scanner_rotation + 1) % len(scanner_scored)
            else:
                scanner_ranked_view = scanner_scored

            # Dedup: don't include scanner results already in catalyst picks
            catalyst_syms = set(s.symbol for s in scored)
            scanner_only = [s for s in scanner_ranked_view if s.symbol not in catalyst_syms]

            # Gate: only promote scanner candidates with a catalyst_score that
            # meets the minimum threshold.  Raw scanner scores (momentum-based)
            # are not comparable to the 0-100 catalyst composite scale.
            scanner_slots = max(0, TRADE_TOP_N - len(scored))
            scanner_promoted = []
            for sa in scanner_only[:scanner_slots]:
                sa_cat = getattr(sa, "catalyst_score", None) or 0.0
                if sa_cat >= MIN_CATALYST_SCORE:
                    scanner_promoted.append(sa)
                else:
                    # Log for diagnostics — this candidate lacks a canonical score
                    log.debug(
                        "scanner_candidate_no_catalyst sym=%s raw_score=%.2f",
                        sa.symbol, getattr(sa, "score", 0.0),
                    )
            scanner_added = scanner_promoted
            _funnel.scan_raw += len(scanner_added)
            _funnel.static_qualified += len(scanner_added)
            _funnel.contract_ok += len(scanner_added)
            for sa in scanner_added:
                _sa_src = getattr(sa, "source", None) or "scanner"
                _source_tracker.record_seen(_sa_src)
                _source_tracker.record_accepted(_sa_src)
            scored.extend(scanner_added)

            if scanner_added:
                print(f"  [SCANNER] Added {len(scanner_added)} diversity candidates")

            active = get_active_symbols(ib)
            current_symbols = [c.symbol for c in scored]

            if (now - last_breadth_ts) >= BREADTH_CACHE_SECONDS or last_breadth_pct is None:
                last_breadth_pct = get_scan_breadth_pct(ib, cached_scan)
                last_breadth_source = "scan"
                if last_breadth_pct is None:
                    last_breadth_pct = get_market_breadth_pct()
                    last_breadth_source = "yahoo" if last_breadth_pct is not None else "n/a"
                last_breadth_ts = now

            high_score_count = 0
            earnings_high_conf_count = 0
            if catalyst_ranking:
                high_score_count = sum(1 for opp in catalyst_ranking if opp.combined_score >= 75)
                earnings_high_conf_count = sum(
                    1
                    for opp in catalyst_ranking
                    if "earnings" in opp.best_catalyst_types and opp.confidence >= 0.9
                )

            # ====== REGIME FILTER (Phase 2) ======
            regime = get_regime(ib, breadth_pct=last_breadth_pct)
            _dash_regime = regime.regime

            # Track regime changes
            last_regime = tracker.regime_log[-1]["regime"] if tracker.regime_log else None
            if regime.regime != last_regime:
                tracker.regime_log.append({"regime": regime.regime, "ts": datetime.now(timezone.utc).isoformat()})

            breadth_trigger = (last_breadth_pct is not None) and (last_breadth_pct >= BREADTH_ADV_THRESHOLD)
            conviction_mode = (
                (high_score_count >= 3
                 or earnings_high_conf_count >= 2
                 or breadth_trigger)
                and regime.regime != "RED"   # Never convict in RED
            )

            # Build conviction reasons for audit trail
            conviction_reasons = []
            if high_score_count >= 3:
                conviction_reasons.append(f"high_scores={high_score_count}>=3")
            if earnings_high_conf_count >= 2:
                conviction_reasons.append(f"earnings_conf={earnings_high_conf_count}>=2")
            if breadth_trigger:
                conviction_reasons.append(f"breadth={last_breadth_pct*100:.1f}%>=65%")

            risk_per_trade_pct = CONVICTION_RISK_PER_TRADE if conviction_mode else BASE_RISK_PER_TRADE
            max_total_open_risk = CONVICTION_MAX_TOTAL_OPEN_RISK if conviction_mode else BASE_MAX_TOTAL_OPEN_RISK
            max_concurrent_positions = (
                CONVICTION_MAX_CONCURRENT_POSITIONS if conviction_mode else BASE_MAX_CONCURRENT_POSITIONS
            )

            # Regime scaling
            if regime.regime == "YELLOW":
                risk_per_trade_pct *= YELLOW_RISK_REDUCTION
                max_concurrent_positions = max(1, max_concurrent_positions - YELLOW_POSITION_PENALTY)

            drawdown_pct = 0.0
            if session_peak_equity > 0:
                drawdown_pct = max(0.0, (session_peak_equity - equity) / session_peak_equity)
                if drawdown_pct >= PEAK_DRAWDOWN_THRESHOLD:
                    risk_per_trade_pct = min(risk_per_trade_pct, PEAK_DRAWDOWN_RISK_PCT)

            if loss_streak_penalty_remaining > 0:
                risk_per_trade_pct *= LOSS_STREAK_RISK_REDUCTION

            # Refresh ATR cache for scored symbols (every 10 min per symbol)
            for cnd in scored:
                sym = cnd.symbol
                last_t = atr_cache_ts.get(sym, 0.0)
                if (now - last_t) >= ATR_CACHE_SECONDS or sym not in atr_cache:
                    try:
                        df_d = get_daily_30d(ib, sym)
                        atr_cache[sym] = atr14_from_daily(df_d)
                        atr_cache_ts[sym] = now
                    except Exception:
                        atr_cache[sym] = atr_cache.get(sym, 0.0)

            # ── Fill detection: upgrade ORDER_PLACED → POSITION_OPEN ──
            newly_filled = get_filled_symbols(ib) - confirmed_fills
            for sym in newly_filled:
                confirmed_fills.add(sym)
                # Find entry price from IB position avgCost
                _fill_entry = 0.0
                _fill_qty = 0
                for p in ib.positions():
                    if p.contract.symbol == sym and p.position != 0:
                        _fill_entry = float(getattr(p, "avgCost", 0.0) or 0.0)
                        _fill_qty = abs(int(p.position))
                        break
                tracker.positions_opened.append({
                    "symbol": sym, "qty": _fill_qty,
                    "entry": round(_fill_entry, 2),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                lifecycle.emit(OrderEvent.ORDER_FILLED, sym,
                              qty=_fill_qty, entry_price=round(_fill_entry, 2),
                              message="confirmed fill from IB positions")
                lifecycle.emit(OrderEvent.POSITION_OPEN, sym,
                              qty=_fill_qty, entry_price=round(_fill_entry, 2))
                dist_analyzer.record_fill(sym, _fill_entry, _fill_qty)
                journal.record_fill(sym, entry_fill=round(_fill_entry, 2),
                                    qty=_fill_qty)
                print(f"[FILL] {sym}: POSITION_OPEN confirmed — qty={_fill_qty} entry=${_fill_entry:.2f}")

            if (now - last_trail_check_ts) >= TRAIL_CHECK_SECONDS:
                last_trail_check_ts = now
                for p in ib.positions():
                    if p.position <= 0:
                        continue
                    sym = p.contract.symbol
                    
                    # Skip if already activated
                    if sym in trail_active_symbols:
                        continue

                    # Get or compute ATR
                    atr = atr_cache.get(sym, 0.0)
                    if atr <= 0:
                        try:
                            df_d = get_daily_30d(ib, sym)
                            atr = atr14_from_daily(df_d)
                            atr_cache[sym] = atr
                        except Exception:
                            continue

                    entry_price = float(getattr(p, "avgCost", 0.0) or 0.0)
                    if entry_price <= 0:
                        continue

                    # Use 1m bars for price (not snapshot) to avoid NaN
                    try:
                        current_px = get_recent_price_1m(ib, sym)
                    except Exception:
                        continue

                    activation_px = entry_price + (TRAIL_ACTIVATE_ATR * atr)
                    
                    # Check if trail should be activated
                    if current_px >= activation_px:
                        # Activate trail
                        trail_amt = atr * TRAIL_ATR_MULT
                        stop_id = stop_order_ids.get(sym)
                        if stop_id:
                            cancel_order_by_id(ib, stop_id)

                        qty = abs(int(p.position))
                        if not armed:
                            print(f"[SIM] Activate trail {sym} qty={qty} trail=${trail_amt:.2f}")
                            trail_active_symbols.add(sym)
                            TRAIL_STATE[sym] = "activated"
                            continue

                        res = place_trailing_stop(ib, sym, qty, trail_amt, tif="DAY")
                        if res.ok:
                            trail_active_symbols.add(sym)
                            TRAIL_STATE[sym] = "activated"
                            if sym not in TRAIL_LOGGED:
                                tracker.trail_activations.append({
                                    "symbol": sym, "trail_amt": round(trail_amt, 2),
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                })
                                lifecycle.emit(OrderEvent.TRAIL_ACTIVATED, sym,
                                              trail_amount=round(trail_amt, 2),
                                              trail_id=res.trail_id)
                                journal.record_trail_activated(
                                    sym, trail_amount=round(trail_amt, 2),
                                    trail_id=res.trail_id)
                                TRAIL_LOGGED.add(sym)
                    else:
                        # Trail not yet activated - log PENDING once
                        if TRAIL_STATE.get(sym) != "pending":
                            TRAIL_STATE[sym] = "pending"
                            profit_pct = ((current_px - entry_price) / entry_price) * 100
                            print(
                                f"[TRAIL_PENDING] {sym} price=${current_px:.2f} entry=${entry_price:.2f} "
                                f"(+{profit_pct:.1f}%) activate_at=${activation_px:.2f}"
                            )

            open_risk_pct, filled_risk_pct, pending_risk_pct = compute_open_risk_pct(ib, equity, atr_cache)
            _n_pos = len([p for p in ib.positions() if p.position != 0])
            _n_wrk = len([t for t in ib.openTrades() if t.orderStatus.status in ('PreSubmitted', 'Submitted')])
            _dash_open_risk, _dash_filled_risk, _dash_pending_risk = open_risk_pct, filled_risk_pct, pending_risk_pct
            _dash_n_pos, _dash_n_wrk = _n_pos, _n_wrk
            tracker.update_risk_watermarks(open_risk_pct, filled_risk_pct, pending_risk_pct, _n_pos, _n_wrk)

            # ── Lifecycle: poll IB order status transitions ──
            try:
                lifecycle.poll_order_status(ib.openTrades())
            except Exception:
                pass

            if open_risk_pct >= max_total_open_risk:
                if (now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS:
                    print(f"Max open risk reached: {open_risk_pct:.3f} (filled={filled_risk_pct:.3f} pending={pending_risk_pct:.3f}) >= {max_total_open_risk:.3f}. No new trades.")
                    last_print_ts = now
                _write_dashboard()
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            should_print = (current_symbols != last_symbols) or ((now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS)

            # Pre-compute SPY momentum once for all candidates
            _spy_df = fetch_spy_5m(ib)
            _spy_mom_30m = calc_momentum(_spy_df, 30) if _spy_df is not None else 0.0

            if should_print:
                engine_status = "🎯 CATALYST PRIMARY" if research_engine else "📊 SCANNER"
                if last_breadth_pct is not None:
                    breadth_pct_display = f"{last_breadth_pct*100:.1f}% ({last_breadth_source})"
                else:
                    breadth_pct_display = "n/a"
                mode_label = "CONVICTION" if conviction_mode else "BASE"
                conviction_audit = f" [{', '.join(conviction_reasons)}]" if conviction_mode else ""
                regime_tag = f"regime={regime.regime}"
                if regime.reasons:
                    regime_tag += f" [{', '.join(regime.reasons[:2])}]"
                print(
                    f"\n--- Loop --- ARMED={armed} mode={mode_label}{conviction_audit} {regime_tag} "
                    f"equity={equity:,.0f} risk={open_risk_pct:.3f}(filled={filled_risk_pct:.3f}+pending={pending_risk_pct:.3f}) active={len(active)} "
                    f"breadth={breadth_pct_display} {engine_status}"
                )
                counters = scanner_stability_counters()
                print(
                    "[SCANNER] counters "
                    f"stale_ignored={counters['scanner_stale_callback_ignored']} "
                    f"generation_resets={counters['scanner_generation_resets']} "
                    f"restart_count={counters['scanner_restart_count']}"
                )
                print(_funnel.log_line())
                print(_broker_sentinel.log_line())
                print(_source_tracker.log_line())
                if is_paper:
                    print(_tp.log_line())
                print(_ledger_log_line())

                # Reset per-cycle throughput counters
                _tp.reset(candidates=len(scored))
                if is_paper:
                    _paper_boost.near_miss_this_loop = False

                brackets_submitted_this_loop = 0  # Throttle: max 1 per loop
                
                # ====== SESSION GATE: skip new entry intents when session blocks ======
                _session_ok, _session_reason = _session_allows_new_entries()
                if not _session_ok:
                    print(f"[SESSION_GATE] new entries blocked: {_session_reason}")
                    _tp.session_gate_skipped = len(scored)

                # ====== REGIME GATE: RED → skip new entries ======
                if regime.regime == "RED":
                    print(f"[REGIME] RED — no new entries. Reasons: {', '.join(regime.reasons)}")
                    _tp.regime_blocked = len(scored)

                for _ci, cand in enumerate(scored):
                    sym = cand.symbol
                    tracker.candidates_checked += 1

                    # Session gate: no entry intents when session certainly rejects
                    if not _session_ok:
                        break  # already counted in session_gate_skipped

                    if sym in invalid_symbols:
                        _tp.invalid_sym_skipped += 1
                        continue

                    # Skip if already active
                    if sym in active:
                        _tp.already_active_skipped += 1
                        continue

                    # Skip recently hyper-rejected symbols to avoid re-check spam every loop
                    last_hs_reject = last_hyper_reject_ts.get(sym, 0.0)
                    last_hs_reason = last_hyper_reject_reason.get(sym, "")
                    recheck_seconds = HYPER_RECHECK_SECONDS
                    if "price below VWAP" in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_VWAP_SECONDS
                    elif "vol_accel" in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_VOL_SECONDS
                    elif " > max " in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_PRICECAP_SECONDS

                    if (now - last_hs_reject) < recheck_seconds:
                        _tp.hyperfilter_skipped += 1
                        _tp.regime_reject_ctx[f"{regime.regime}:hyperfilter_cooldown"] = _tp.regime_reject_ctx.get(f"{regime.regime}:hyperfilter_cooldown", 0) + 1
                        continue

                    # Regime RED: block new entries entirely
                    if regime.regime == "RED":
                        _tp.carry_forward += len(scored) - _ci - 1
                        break

                    # Skip if max positions reached
                    if len(active) >= max_concurrent_positions:
                        print("Max concurrent positions reached.")
                        _tp.carry_forward += len(scored) - _ci - 1
                        break

                    # ====== SCORE THRESHOLD CHECK (CATALYST ONLY) ======
                    cat_score = getattr(cand, 'catalyst_score', None) or 0.0
                    if cat_score < MIN_CATALYST_SCORE:
                        _tp.catalyst_low_skipped += 1
                        _tp.regime_reject_ctx[f"{regime.regime}:catalyst_low"] = _tp.regime_reject_ctx.get(f"{regime.regime}:catalyst_low", 0) + 1
                        continue

                    # ====== FIX B: UNIVERSE FILTER (STOCKS ONLY) ======
                    is_valid, reason = is_valid_stock_contract(ib, sym, valid_contracts=valid_contracts)
                    if not is_valid:
                        if sym not in invalid_symbols_logged:
                            print(f"[REJECT] {sym}: not tradeable ({reason})")
                            invalid_symbols_logged.add(sym)
                        invalid_symbols.add(sym)
                        _tp.invalid_sym_skipped += 1
                        continue

                    # ====== QUANT VERIFICATION (Phase 2) ======
                    try:
                        qm = compute_candidate_metrics(ib, sym, spy_mom_30m=_spy_mom_30m)
                    except Exception as e:
                        print(f"[SKIP] {sym}: quant metrics failed ({e})")
                        _tp.quant_fail_skipped += 1
                        continue

                    if not qm.ok:
                        print(f"[SKIP] {sym}: {qm.error}")
                        _tp.quant_fail_skipped += 1
                        continue

                    dist_analyzer.record_checked(sym, cat_score, qm)

                    # ---- Hyper-swing gates ----
                    hs_cfg = dict(
                        PRICE_MIN=CFG_PRICE_MIN,
                        PRICE_MAX=CFG_PRICE_MAX,
                        MIN_ATR_PCT=MIN_ATR_PCT,
                        MIN_ADV20_DOLLARS=MIN_ADV20_DOLLARS,
                        MIN_VOLUME_ACCEL=(PAPER_MIN_VOLUME_ACCEL if is_paper else MIN_VOLUME_ACCEL),
                        MIN_RS_VS_SPY=MIN_RS_VS_SPY,
                        REQUIRE_ABOVE_VWAP=True,
                        PRICE_MAX_ALLOWLIST=PRICE_MAX_ALLOWLIST,
                    )
                    hs_pass, hs_reason = passes_hyper_swing_filters(qm, config=hs_cfg)
                    gate_tag = "MOMENTUM"
                    gate_reason = hs_reason
                    gate_pass = hs_pass

                    bounce_pass = False
                    bounce_reason = ""
                    if not hs_pass and ENABLE_BOUNCE_MODE and regime.regime in ("GREEN", "YELLOW"):
                        bounce_reject_reasons = []
                        playbook_sample = max(0, int(getattr(qm, "playbook_sample_size_5d", 0) or 0))

                        if playbook_sample < BOUNCE_MIN_SAMPLE_SIZE:
                            # Informational only — log but do NOT gate.
                            # Fresh system has zero history; blocking bounce on sample size
                            # makes the fallback path permanently unreachable.
                            print(
                                f"[BOUNCE_INFO] {sym}: playbook_n {playbook_sample} < min {BOUNCE_MIN_SAMPLE_SIZE} "
                                f"(informational — not gating)")

                        if qm.price < CFG_PRICE_MIN:
                            bounce_reject_reasons.append(f"price ${qm.price:.2f} < min ${CFG_PRICE_MIN}")
                        if qm.price > CFG_PRICE_MAX and sym not in PRICE_MAX_ALLOWLIST:
                            bounce_reject_reasons.append(f"price ${qm.price:.2f} > max ${CFG_PRICE_MAX}")
                        if qm.atr_percent < MIN_ATR_PCT:
                            bounce_reject_reasons.append(f"atr% {qm.atr_percent:.3f} < {MIN_ATR_PCT}")
                        if qm.adv20_dollars < MIN_ADV20_DOLLARS:
                            bounce_reject_reasons.append(
                                f"adv20 ${qm.adv20_dollars/1e6:.1f}M < ${MIN_ADV20_DOLLARS/1e6:.0f}M"
                            )
                        if qm.playbook_win_rate_5d < BOUNCE_MIN_PLAYBOOK_WIN_RATE:
                            bounce_reject_reasons.append(
                                f"playbook_wr {qm.playbook_win_rate_5d*100:.1f}% < {BOUNCE_MIN_PLAYBOOK_WIN_RATE*100:.1f}%"
                            )
                        if qm.playbook_expectancy_5d < BOUNCE_MIN_PLAYBOOK_EXPECTANCY:
                            bounce_reject_reasons.append(
                                f"playbook_exp {qm.playbook_expectancy_5d*100:+.2f}% < {BOUNCE_MIN_PLAYBOOK_EXPECTANCY*100:+.2f}%"
                            )
                        if qm.playbook_score < BOUNCE_MIN_PLAYBOOK_SCORE:
                            bounce_reject_reasons.append(
                                f"playbook_score {qm.playbook_score:.1f} < {BOUNCE_MIN_PLAYBOOK_SCORE:.1f}"
                            )
                        if abs(min(0.0, qm.playbook_mae_5d)) > BOUNCE_MAX_MAE_ABS:
                            bounce_reject_reasons.append(
                                f"playbook_mae {qm.playbook_mae_5d*100:+.2f}% worse than -{BOUNCE_MAX_MAE_ABS*100:.2f}%"
                            )

                        if not bounce_reject_reasons:
                            bounce_pass = True
                            bounce_reason = (
                                f"[BOUNCE_PASS] {sym}: "
                                f"wr={qm.playbook_win_rate_5d*100:.1f}% exp={qm.playbook_expectancy_5d*100:+.2f}% "
                                f"score={qm.playbook_score:.1f} mae={qm.playbook_mae_5d*100:+.2f}% n={playbook_sample} "
                                f"atr%={qm.atr_percent*100:.2f}% adv=${qm.adv20_dollars/1e6:.0f}M regime={regime.regime}"
                            )
                        else:
                            bounce_reason = f"[BOUNCE_REJECT] {' | '.join(bounce_reject_reasons)}"

                    if not hs_pass and bounce_pass:
                        gate_tag = "BOUNCE"
                        gate_reason = bounce_reason
                        gate_pass = True

                    if not gate_pass:
                        last_hyper_reject_ts[sym] = now
                        last_hyper_reject_reason[sym] = f"{hs_reason} || {bounce_reason}" if bounce_reason else hs_reason
                        tracker.hyper_rejections.append({"symbol": sym, "reason": hs_reason})
                        print(f"[HYPER_FILTER] {sym}: {hs_reason}")
                        if bounce_reason:
                            tracker.bounce_rejections.append({"symbol": sym, "reason": bounce_reason})
                            print(f"[BOUNCE_FILTER] {sym}: {bounce_reason}")
                        _tp.hyperfilter_skipped += 1
                        _tp.regime_reject_ctx[f"{regime.regime}:hyperfilter_{gate_tag.lower()}"] = _tp.regime_reject_ctx.get(f"{regime.regime}:hyperfilter_{gate_tag.lower()}", 0) + 1
                        continue
                    last_hyper_reject_ts.pop(sym, None)
                    last_hyper_reject_reason.pop(sym, None)
                    print(gate_reason)

                    # ---- Unified score ----
                    unified = (UNIFIED_CATALYST_WEIGHT * cat_score
                               + UNIFIED_QUANT_WEIGHT * qm.quant_score)
                    if gate_tag == "BOUNCE":
                        required_unified = BOUNCE_MIN_UNIFIED_SCORE
                    else:
                        required_unified = CONVICTION_MIN_UNIFIED_SCORE if conviction_mode else BASE_MIN_UNIFIED_SCORE

                    # Paper-only boost: apply after regime gate (RED already checked above)
                    if is_paper:
                        _base_req = required_unified
                        required_unified = _paper_boost_effective_min(required_unified)
                        # Near-miss: candidate within delta of base threshold
                        if (not _paper_boost.boost_active
                                and unified >= _base_req - _PAPER_BOOST_DELTA
                                and unified < _base_req):
                            _paper_boost.near_miss_this_loop = True

                    if unified < required_unified:
                        tracker.score_rejections.append({
                            "symbol": sym,
                            "reason": f"unified {unified:.1f} < {required_unified:.1f} ({gate_tag})",
                        })
                        print(
                            f"[SCORE] {sym}: unified={unified:.1f} < {required_unified:.1f} "
                            f"(mode={gate_tag} cat={cat_score:.0f} quant={qm.quant_score:.0f})"
                        )
                        _tp.unified_score_skipped += 1
                        _tp.regime_reject_ctx[f"{regime.regime}:unified_score_{gate_tag.lower()}"] = _tp.regime_reject_ctx.get(f"{regime.regime}:unified_score_{gate_tag.lower()}", 0) + 1
                        continue

                    # ---- Candidate summary ----
                    dist_analyzer.record_signal(sym, unified, cat_score,
                                               qm.quant_score, gate_tag, qm)
                    tracker.signals.append({
                        "symbol": sym, "unified_score": round(unified, 1),
                        "catalyst_score": round(cat_score, 0),
                        "quant_score": round(qm.quant_score, 0),
                        "gate": gate_tag,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    lifecycle.emit(OrderEvent.SIGNAL_SCORE, sym,
                                  unified_score=round(unified, 1),
                                  catalyst_score=round(cat_score, 0),
                                  quant_score=round(qm.quant_score, 0),
                                  gate=gate_tag)
                    journal.create_trade_record(
                        sym,
                        unified_score=round(unified, 1),
                        catalyst_score=round(cat_score, 0),
                        quant_score=round(qm.quant_score, 0),
                        gate_type=gate_tag,
                        vol_accel=round(qm.volume_accel, 3),
                        atr_pct=round(qm.atr_percent, 6),
                        rs_30m_delta=round(qm.rel_strength_vs_spy, 6),
                        momentum_30m=round(qm.momentum_30m, 6),
                        adv20_dollars=round(qm.adv20_dollars, 0),
                        regime=regime.regime,
                    )
                    print(
                        f"  ✅ [{gate_tag}] {sym}  unified={unified:.1f} cat={cat_score:.0f} quant={qm.quant_score:.0f}  "
                        f"mom30={qm.momentum_30m*100:+.2f}% vol_accel={qm.volume_accel:.2f} "
                        f"RS30mΔ={qm.rel_strength_vs_spy*100:+.2f}% atr%={qm.atr_percent*100:.2f}% "
                        f"adv=${qm.adv20_dollars/1e6:.0f}M playbook={qm.playbook_score:.0f} n={qm.playbook_sample_size_5d}"
                    )

                    # ====== USE QUANT METRICS for price/ATR ======
                    px = qm.price
                    atr = qm.atr14
                    # Sync ATR cache from quant verification
                    if atr > 0:
                        atr_cache[sym] = atr
                    if not math.isfinite(atr) or atr <= 0:
                        _tp.sizing_fail_skipped += 1
                        continue

                    # ====== CALCULATE SIZING ======
                    risk_dollars = equity * risk_per_trade_pct
                    qty = int(risk_dollars // atr)
                    if qty <= 0:
                        _tp.sizing_fail_skipped += 1
                        continue

                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    stop_loss = entry - (STOP_LOSS_R * atr)
                    trail_amt = 0.0  # Delayed trailing stop activation
                    trail_activate_px = entry + (TRAIL_ACTIVATE_ATR * atr)
                    
                    # Triple-check before submission
                    if not math.isfinite(entry) or entry <= 0:
                        print(f"[VALIDATION] {sym}: entry price invalid (${entry})")
                        _tp.sizing_fail_skipped += 1
                        continue
                    
                    if not math.isfinite(stop_loss) or stop_loss <= 0:
                        print(f"[VALIDATION] {sym}: stop loss invalid (${stop_loss})")
                        _tp.sizing_fail_skipped += 1
                        continue

                    if atr <= 0:
                        print(f"[VALIDATION] {sym}: ATR invalid ({atr})")
                        _tp.sizing_fail_skipped += 1
                        continue
                    
                    if qty <= 0:
                        print(f"[VALIDATION] {sym}: qty invalid ({qty})")
                        _tp.sizing_fail_skipped += 1
                        continue

                    # ---- Log trade intent ----
                    dist_analyzer.record_intent(sym, entry, risk_per_trade_pct, qm)
                    tracker.intents.append({
                        "symbol": sym, "qty": qty,
                        "entry": round(entry, 2), "stop": round(stop_loss, 2),
                        "trail_activate": round(trail_activate_px, 2),
                        "risk_pct": round(risk_per_trade_pct, 4),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    _tp.intents_emitted += 1
                    lifecycle.emit(OrderEvent.TRADE_INTENT_CREATED, sym,
                                  qty=qty, entry_price=round(entry, 2),
                                  stop_price=round(stop_loss, 2),
                                  risk_pct=round(risk_per_trade_pct, 4))
                    journal.record_intent(
                        sym, qty=qty, entry_limit=round(entry, 2),
                        stop_price=round(stop_loss, 2),
                        trail_activation_price=round(trail_activate_px, 2),
                        risk_pct=round(risk_per_trade_pct, 4),
                    )

                    # ====== CHECK DAILY KILL SWITCH ======
                    if is_kill_switch_active(ib):
                        tracker.risk_rejected.append({"symbol": sym, "reason": "kill_switch_daily_loss"})
                        lifecycle.emit(OrderEvent.RISK_REJECTED, sym,
                                      message="kill_switch_daily_loss")
                        _tp.throttled += 1
                        continue
                    
                    # ====== CHECK THROTTLE: COOLDOWN PER SYMBOL ======
                    last_bracket_time = last_bracket_ts.get(sym, 0.0)
                    if now - last_bracket_time < COOLDOWN_SECONDS_PER_SYMBOL:
                        cooldown_remain = COOLDOWN_SECONDS_PER_SYMBOL - (now - last_bracket_time)
                        print(f"[THROTTLE] {sym}: cooldown active ({cooldown_remain:.0f}s remaining)")
                        _tp.throttled += 1
                        continue

                    # ====== CHECK THROTTLE: GLOBAL BRACKET COOLDOWN ======
                    if BRACKET_COOLDOWN_SECONDS > 0 and (now - last_bracket_attempt_ts) < BRACKET_COOLDOWN_SECONDS:
                        cooldown_remain = BRACKET_COOLDOWN_SECONDS - (now - last_bracket_attempt_ts)
                        print(f"[THROTTLE] Bracket cooldown active ({cooldown_remain:.0f}s remaining)")
                        _tp.throttled += 1
                        _tp.carry_forward += len(scored) - _ci - 1
                        break
                    
                    # ====== CHECK THROTTLE: MAX 1 PER LOOP ======
                    if brackets_submitted_this_loop >= MAX_NEW_BRACKETS_PER_LOOP:
                        print(f"[THROTTLE] {sym}: max {MAX_NEW_BRACKETS_PER_LOOP} bracket(s) per loop reached")
                        _tp.throttled += 1
                        _tp.carry_forward += len(scored) - _ci - 1
                        break
                    
                    # ====== CHECK SYMBOL LOCK: NO DUPLICATE ENTRIES ======
                    locked, lock_reason = is_symbol_locked(ib, sym, symbol_lock_ts, now, SYMBOL_COOLDOWN_SECONDS)
                    if locked:
                        print(f"[LOCK] {sym}: {lock_reason}, skipping")
                        _tp.throttled += 1
                        continue
                    
                    # ---- Risk check passed ----
                    tracker.risk_approved.append({
                        "symbol": sym, "qty": qty,
                        "entry": round(entry, 2), "stop": round(stop_loss, 2),
                        "open_risk_pct": round(open_risk_pct, 4),
                        "filled_risk_pct": round(filled_risk_pct, 4),
                        "pending_risk_pct": round(pending_risk_pct, 4),
                        "regime": regime.regime,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    lifecycle.emit(OrderEvent.RISK_APPROVED, sym,
                                  qty=qty, entry_price=round(entry, 2),
                                  stop_price=round(stop_loss, 2),
                                  risk_pct=round(open_risk_pct, 4))

                    # ====== SIM MODE ======
                    if not armed:
                        tracker.orders_placed.append({
                            "symbol": sym, "qty": qty,
                            "entry": round(entry, 2), "stop": round(stop_loss, 2),
                            "ok": True, "message": "SIM",
                            "ts": datetime.now(timezone.utc).isoformat(),
                        })
                        lifecycle.emit(OrderEvent.ORDER_PLACED, sym,
                                      qty=qty, entry_price=round(entry, 2),
                                      stop_price=round(stop_loss, 2),
                                      status="SIM")
                        journal.record_order_submitted(
                            sym, status="submitted_unfilled")
                        print(
                            f"[SIM] {sym} unified={unified:.0f} qty={qty} entry=${entry:.2f} stop=${stop_loss:.2f} "
                            f"trail=PENDING (>{trail_activate_px:.2f}) risk={risk_per_trade_pct:.3%}"
                        )
                        last_bracket_attempt_ts = now
                        symbol_lock_ts[sym] = now  # Lock immediately after attempt
                        brackets_submitted_this_loop += 1
                        _tp.carry_forward += max(0, len(scored) - _ci - 1)
                        break

                    # ====== SUBMIT BRACKET (ARMED MODE) ======
                    params = BracketParams(
                        symbol=sym, qty=qty,
                        entry_limit=entry, stop_loss=stop_loss, trail_amount=trail_amt,
                        tif="DAY"
                    )
                    res = place_limit_tp_trail_bracket(ib, params)
                    _parent = getattr(res, 'parent_id', None)
                    _stop = getattr(res, 'stop_id', None) or getattr(res, 'tp_id', None)
                    _trail = getattr(res, 'trail_id', None)
                    _degraded = getattr(res, 'degraded', False)
                    if _degraded:
                        tracker.degraded_brackets += 1
                    _rth = is_regular_trading_hours()
                    _order_status = "WORKING" if _rth else "QUEUED_NEXT_SESSION"
                    tracker.orders_placed.append({
                        "symbol": sym, "qty": qty,
                        "entry": round(entry, 2), "stop": round(stop_loss, 2),
                        "ok": res.ok, "message": res.message,
                        "parent_id": _parent, "stop_id": _stop, "trail_id": _trail,
                        "degraded": _degraded, "status": _order_status,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    if res.ok:
                        lifecycle.emit(OrderEvent.ORDER_PLACED, sym,
                                      qty=qty, entry_price=round(entry, 2),
                                      stop_price=round(stop_loss, 2),
                                      parent_id=_parent, stop_id=_stop,
                                      trail_id=_trail, status=_order_status)
                        journal.record_order_submitted(
                            sym, parent_id=_parent, stop_id=_stop,
                            trail_id=_trail, degraded=_degraded,
                            queued=not _rth,
                            status="submitted_unfilled")
                        if _degraded:
                            lifecycle.emit(OrderEvent.BRACKET_DEGRADED, sym,
                                          parent_id=_parent, stop_id=_stop,
                                          trail_id=_trail,
                                          message="child leg failed")
                        if _rth:
                            lifecycle.emit(OrderEvent.ORDER_WORKING, sym,
                                          order_id=_parent, status="Submitted")
                        else:
                            lifecycle.emit(OrderEvent.ORDER_QUEUED_NEXT_SESSION, sym,
                                          qty=qty, entry_price=round(entry, 2),
                                          message="DAY order queued outside RTH")
                            print(f"  [QUEUED] {sym}: DAY order queued for next regular session")
                        if _parent:
                            lifecycle.register_order(sym, _parent)
                    else:
                        tracker.errors.append({"type": "order_rejected", "symbol": sym,
                                               "message": res.message,
                                               "ts": datetime.now(timezone.utc).isoformat()})
                        lifecycle.emit(OrderEvent.SYSTEM_ERROR, sym,
                                      message=f"order_rejected: {res.message}")
                    print(f"[IB] {sym} -> {res.ok} {res.message}")
                    last_bracket_attempt_ts = now
                    symbol_lock_ts[sym] = now  # Lock immediately after attempt (success or fail)
                    brackets_submitted_this_loop += 1
                    
                    if res.ok:
                        active.add(sym)
                        last_bracket_ts[sym] = now
                        stop_order_ids[sym] = getattr(res, 'stop_id', None) or getattr(res, 'tp_id', None) or 0
                        entry_atr_by_symbol[sym] = atr
                        if loss_streak_penalty_remaining > 0:
                            loss_streak_penalty_remaining -= 1

                    # After one attempt (success or fail), do not attempt more symbols this loop
                    _tp.carry_forward += max(0, len(scored) - _ci - 1)
                    break

                # Throughput: emit attribution log, reconciliation, and boost state
                _tp_reconcile(regime.regime)
                if is_paper:
                    print(_tp.log_line())
                    _paper_boost_tick()
                print(_ledger_log_line())

                last_symbols = current_symbols
                last_print_ts = now

            _write_dashboard()
            elapsed = time.time() - loop_start
            time.sleep(max(0.0, LOOP_SECONDS - elapsed))

    except KeyboardInterrupt:
        print("\nStopping live loop.")
    finally:
        # Capture end equity
        try:
            tracker.end_equity = get_equity(ib)
        except Exception:
            tracker.end_equity = tracker.start_equity

        # Capture unrealized PnL snapshot
        try:
            from src.risk.daily_pnl_manager import DailyPnLManager
            _pnl_mgr = DailyPnLManager()
            tracker.unrealized_pnl = _pnl_mgr.get_unrealized_pnl(ib)
        except Exception:
            pass

        # Check for closed positions since session start
        try:
            closed = load_closed_trades()
            session_start_ts = tracker.start_ts
            for t in closed:
                t_ts = t.get("close_time") or t.get("ts") or ""
                pnl_val = t.get("pnl", 0.0)
                _sym = t.get("symbol", "?")
                tracker.positions_closed.append({
                    "symbol": _sym,
                    "pnl": pnl_val,
                    "close_reason": t.get("close_reason", "unknown"),
                    "ts": t_ts,
                })
                tracker.realized_pnl += pnl_val
                lifecycle.emit(OrderEvent.POSITION_CLOSED, _sym,
                              pnl=round(pnl_val, 2),
                              message=t.get("close_reason", "unknown"))
        except Exception:
            pass

        # Count cancelled orders (orders that were submitted OK but not filled)
        try:
            for trade in ib.openTrades():
                if trade.orderStatus.status == "Cancelled":
                    tracker.orders_cancelled += 1
                    _sym = trade.contract.symbol
                    _oid = trade.order.orderId
                    lifecycle.emit(OrderEvent.ORDER_CANCELLED, _sym,
                                  order_id=_oid, message="detected at shutdown")
        except Exception:
            pass

        # ── Telemetry Summary (console + file) ──────────────────
        # Finalize trade journal before summaries
        try:
            _closed_for_journal = load_closed_trades()
        except Exception:
            _closed_for_journal = []
        try:
            _open_syms = get_filled_symbols(ib)
        except Exception:
            _open_syms = set()
        try:
            journal.finalize_session(_closed_for_journal, _open_syms)
        except Exception as e:
            print(f"[ERROR] journal.finalize_session: {e}")

        for _summary_fn in (tracker.print_summary, lifecycle.print_summary,
                            journal.print_summary, dist_analyzer.print_summary):
            try:
                _summary_fn()
            except Exception as e:
                print(f"[ERROR] {_summary_fn.__qualname__}: {e}")

        # ── Write order lifecycle artifact ───────────────────────
        try:
            lc_path = lifecycle.write_json("logs")
            if lc_path:
                print(f"📋 Order lifecycle  → {lc_path}")
        except Exception as e:
            print(f"[ERROR] Failed to write order lifecycle: {e}")

        # ── Write trade journal artifact ─────────────────────────
        try:
            jcsv = journal.write_csv("logs")
            jjson = journal.write_json("logs")
            if jcsv:
                print(f"📓 Trade journal CSV  → {jcsv}")
            if jjson:
                print(f"📓 Trade journal JSON → {jjson}")
        except Exception as e:
            print(f"[ERROR] Failed to write trade journal: {e}")

        # ── Write signal distribution artifact ──────────────────
        try:
            dist_path = dist_analyzer.write_json("logs")
            if dist_path:
                print(f"📊 Signal distribution → {dist_path}")
            csv_path = dist_analyzer.write_csv("logs")
            if csv_path:
                print(f"📊 Signal distribution CSV → {csv_path}")
        except Exception as e:
            print(f"[ERROR] Failed to write signal distribution: {e}")

        # ── Write session_report.json + session_telemetry.json ───
        if _ENABLE_SESSION_REPORT:
            report_path = Path("data/reports")
            report_path.mkdir(parents=True, exist_ok=True)
            ts_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

            report = tracker.to_report()
            report["lifecycle_events"] = lifecycle.summary()["events"]
            report_file = report_path / f"session_report_{ts_stamp}.json"
            canonical = report_path / "session_report.json"

            telemetry = tracker.build_telemetry()
            telemetry["lifecycle"] = lifecycle.summary()
            telemetry_file = report_path / f"session_telemetry_{ts_stamp}.json"
            canonical_telem = report_path / "session_telemetry.json"

            try:
                for fpath in (report_file, canonical):
                    with open(fpath, "w") as f:
                        json.dump(report, f, indent=2, default=str)
                for fpath in (telemetry_file, canonical_telem):
                    with open(fpath, "w") as f:
                        json.dump(telemetry, f, indent=2, default=str)
                print(f"\n📄 Session report   → {report_file}")
                print(f"📄 Session telemetry → {telemetry_file}")
                print(f"📄 Latest at {canonical} / {canonical_telem}")
            except Exception as e:
                print(f"[ERROR] Failed to write session reports: {e}")

        # ── Final dashboard snapshot ────────────────────────────
        try:
            _write_dashboard()
        except Exception:
            pass

        # Clean up: give pending async tasks time to complete
        ib.sleep(0.2)
        
        # Cancel pending async tasks before disconnect
        import asyncio
        try:
            pending = asyncio.all_tasks()
            for task in pending:
                task.cancel()
        except:
            pass
        
        # Force close connection
        try:
            ib.client.disconnect()
        except:
            pass
        
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
