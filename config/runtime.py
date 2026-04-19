 
import os
import logging

_log = logging.getLogger("trade_labs.runtime")


# ── Typed env helpers ─────────────────────────────────────────────────

def env_bool(name: str, default: bool) -> bool:
    """Read an env var as bool.  Truthy: '1', 'true', 'yes' (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def env_int(name: str, default: int) -> int:
    """Read an env var as int, falling back to *default* on missing/unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def env_float(name: str, default: float) -> float:
    """Read an env var as float, falling back to *default* on missing/unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# ── Core identity ────────────────────────────────────────────────────

def mode() -> str:
    return os.getenv("TRADE_LABS_MODE", "PAPER").upper()

def is_paper() -> bool:
    return mode() == "PAPER"

def execution_backend() -> str:
    # SIM (default) or IB
    return os.getenv("TRADE_LABS_EXECUTION_BACKEND", "SIM").upper()

def is_armed() -> bool:
    # Must be EXACTLY "1" to allow broker submission
    return os.getenv("TRADE_LABS_ARMED", "0") == "1"


# ── After-hours paper-test configuration ─────────────────────────────
#
# Set UTS_PAPER_AH_TEST=1 to flip a single master switch that enables
# extended-hours paper trading with synthetic quotes in SIM mode.
# Every derived flag below can still be overridden individually via its
# own env var.
#
# Usage:
#   export UTS_PAPER_AH_TEST=1
#   python -u -m src.live_loop_10s
#
# This is a paper/SIM-only convenience toggle.  Production sessions
# must never rely on it — the underlying flags default to safe values
# when UTS_PAPER_AH_TEST is unset.

PAPER_AH_TEST: bool = env_bool("UTS_PAPER_AH_TEST", False)

allow_extended: bool = env_bool("UTS_ALLOW_EXTENDED", PAPER_AH_TEST)
ah_entry_enabled: bool = env_bool("UTS_AH_ENTRY_ENABLED", PAPER_AH_TEST)

synthetic_ok: bool = env_bool(
    "UTS_SYNTHETIC_OK",
    PAPER_AH_TEST and is_paper() and execution_backend() == "SIM",
)

require_live_quotes: bool = env_bool(
    "UTS_REQUIRE_LIVE_QUOTES",
    not synthetic_ok,
)

ah_max_open_pos: int = env_int(
    "UTS_MAX_OPEN_POS",
    1 if PAPER_AH_TEST else 5,
)

ah_risk_per_trade: float = env_float(
    "UTS_RISK_PER_TRADE",
    5.0 if PAPER_AH_TEST else 0.5,
)

ah_armed: bool = env_bool("UTS_ARMED", PAPER_AH_TEST)


# ── Resolved Session Config (single source of truth) ─────────────────
#
# Every arm must receive config through this object rather than reading
# env vars independently.  The startup table logs raw → parsed → effective
# for every AH-relevant flag so divergent interpretation is impossible.

class _ConfigEntry:
    """One config key's resolution chain."""
    __slots__ = ("key", "raw_env", "parsed", "default_source", "effective")

    def __init__(self, key: str, raw_env, parsed, default_source: str, effective):
        self.key = key
        self.raw_env = raw_env
        self.parsed = parsed
        self.default_source = default_source
        self.effective = effective

    def __repr__(self):
        return (
            f"{self.key:<28s} | {str(self.raw_env):<12s} | "
            f"{str(self.parsed):<8s} | {self.default_source:<20s} | {self.effective}"
        )


class ResolvedSessionConfig:
    """Canonical runtime config resolved once at startup.

    Fields mirror `config.runtime` module-level globals but are frozen
    after construction so no arm can silently diverge.
    """
    __slots__ = (
        "mode", "paper", "backend", "armed",
        "paper_ah_test", "allow_extended", "ah_entry_enabled",
        "synthetic_ok", "require_live_quotes", "ah_armed",
        "ah_max_open_pos", "ah_risk_per_trade",
        "_entries",
    )

    def __init__(self):
        self.mode = mode()
        self.paper = is_paper()
        self.backend = execution_backend()
        self.armed = is_armed()

        self.paper_ah_test = PAPER_AH_TEST
        self.allow_extended = allow_extended
        self.ah_entry_enabled = ah_entry_enabled
        self.synthetic_ok = synthetic_ok
        self.require_live_quotes = require_live_quotes
        self.ah_armed = ah_armed
        self.ah_max_open_pos = ah_max_open_pos
        self.ah_risk_per_trade = ah_risk_per_trade

        self._entries = self._build_entries()

    def _build_entries(self) -> list:
        """Build resolution chain entries for every AH-critical flag."""
        def _entry(key, env_name, parsed_val, default_source):
            raw = os.environ.get(env_name)
            return _ConfigEntry(
                key=key,
                raw_env=raw if raw is not None else "<unset>",
                parsed=parsed_val,
                default_source=default_source,
                effective=parsed_val,
            )

        return [
            _entry("mode",               "TRADE_LABS_MODE",             self.mode,               "PAPER"),
            _entry("backend",            "TRADE_LABS_EXECUTION_BACKEND", self.backend,            "SIM"),
            _entry("armed",              "TRADE_LABS_ARMED",            self.armed,              "False"),
            _entry("paper_ah_test",      "UTS_PAPER_AH_TEST",          self.paper_ah_test,      "False"),
            _entry("allow_extended",     "UTS_ALLOW_EXTENDED",          self.allow_extended,     f"paper_ah_test={self.paper_ah_test}"),
            _entry("ah_entry_enabled",   "UTS_AH_ENTRY_ENABLED",       self.ah_entry_enabled,   f"paper_ah_test={self.paper_ah_test}"),
            _entry("synthetic_ok",       "UTS_SYNTHETIC_OK",            self.synthetic_ok,       f"ah_test&paper&sim={self.paper_ah_test and self.paper and self.backend == 'SIM'}"),
            _entry("require_live_quotes","UTS_REQUIRE_LIVE_QUOTES",     self.require_live_quotes,f"not synthetic_ok={not self.synthetic_ok}"),
            _entry("ah_armed",           "UTS_ARMED",                   self.ah_armed,           f"paper_ah_test={self.paper_ah_test}"),
            _entry("ah_max_open_pos",    "UTS_MAX_OPEN_POS",           self.ah_max_open_pos,    f"{'1' if self.paper_ah_test else '5'}"),
            _entry("ah_risk_per_trade",  "UTS_RISK_PER_TRADE",         self.ah_risk_per_trade,  f"{'5.0' if self.paper_ah_test else '0.5'}"),
        ]

    def log_startup_table(self):
        """Log the full resolution table at startup."""
        header = f"{'key':<28s} | {'raw_env':<12s} | {'parsed':<8s} | {'default_source':<20s} | effective"
        sep = "-" * len(header)
        _log.info("── ResolvedSessionConfig startup table ──")
        _log.info(header)
        _log.info(sep)
        for e in self._entries:
            _log.info(str(e))
        _log.info(sep)

    def assert_ah_coherence(self):
        """Hard-fail if AH paper-test overrides disagree with effective values.

        Call this at startup so config-propagation bugs are caught before
        any arm processes data.
        """
        if not self.paper_ah_test:
            return  # assertions only apply when AH test is requested

        errors = []
        if not self.allow_extended:
            errors.append("allow_extended is False but PAPER_AH_TEST is True")
        if not self.ah_entry_enabled:
            errors.append("ah_entry_enabled is False but PAPER_AH_TEST is True")
        if not self.synthetic_ok and self.paper and self.backend == "SIM":
            errors.append("synthetic_ok is False but PAPER+SIM+AH_TEST should enable it")
        if self.require_live_quotes and self.synthetic_ok:
            errors.append("require_live_quotes is True but synthetic_ok is also True — contradictory")
        if not self.ah_armed:
            errors.append("ah_armed is False but PAPER_AH_TEST is True")

        if errors:
            for e in errors:
                _log.error("CONFIG COHERENCE FAIL: %s", e)
            raise RuntimeError(
                f"ResolvedSessionConfig: {len(errors)} coherence failure(s) in AH paper-test mode: "
                + "; ".join(errors)
            )

        _log.info("ResolvedSessionConfig AH coherence check PASSED")


# Module-level singleton — created on first import, available to all arms.
_resolved_config: ResolvedSessionConfig | None = None


def get_resolved_config() -> ResolvedSessionConfig:
    """Return the singleton ResolvedSessionConfig, creating it on first call."""
    global _resolved_config
    if _resolved_config is None:
        _resolved_config = ResolvedSessionConfig()
    return _resolved_config
