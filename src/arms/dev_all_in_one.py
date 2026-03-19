"""
Dev All-in-One — run every arm in a single process (no Redis required).

Starts ingest, signal, risk, execution, and monitor arms as daemon
threads sharing a single :class:`~src.bus.local_bus.LocalBus`.

Usage::

    python -m src.arms.dev_all_in_one

Press Ctrl+C to stop all arms.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

# ── Force local bus before any arm import ────────────────────────────
os.environ.setdefault("BUS_BACKEND", "local")

from src.bus.local_bus import LocalBus
from src.bus.bus_factory import set_shared_bus
from src.monitoring.logger import get_logger

log = get_logger("dev_runner")

_CERT_MODE = os.environ.get(
    "TL_CERT_MODE", "false"
).lower() in ("1", "true", "yes")

_BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║         TRADE-LABS  ·  Dev All-in-One Runner             ║
║                                                          ║
║  Arms : ingest · signal · risk · execution · monitor     ║
║  Bus  : LocalBus (in-memory, no Redis)                   ║
║  Mode : {mode:<8s}                                       ║
║  Cert : {cert:<8s}                                       ║
║                                                          ║
║  Press Ctrl+C to stop                                    ║
╚══════════════════════════════════════════════════════════╝
"""

_FIRST_PAPER = os.environ.get(
    "TL_FIRST_PAPER_SESSION", "false"
).lower() in ("1", "true", "yes")

_running = True
_start_ts: float = 0.0

_last_heartbeat: dict[str, float] = {}
_HEARTBEAT_TIMEOUT_S = 60.0  # arm considered hung if no heartbeat for 60s


def _shutdown(signum, _frame):
    """Propagate stop to all arms by flipping their _running flags."""
    global _running
    if not _running:
        return  # avoid double-print
    _running = False
    log.info("Shutdown signal received (%s) — stopping all arms…", signum)

    # Each arm module exposes a module-level _running flag.
    # Flip them all so their while-loops exit.
    for mod in (_ingest, _signal, _risk, _execution, _monitor):
        if mod is not None and hasattr(mod, "_running"):
            mod._running = False
        # Ensure cooperative stop_event is set (ingest uses this)
        if mod is not None and hasattr(mod, "_stop_event"):
            mod._stop_event.set()


def _run_arm(name: str, entry_fn):
    """Wrapper executed inside a daemon thread.

    ``signal.signal()`` only works on the main thread, so we patch it
    to a no-op for the duration of the arm's ``main()`` call.
    """
    import signal as _sig_mod

    _orig = _sig_mod.signal

    def _noop_signal(signalnum, handler):           # type: ignore[override]
        """Silently ignore signal registration when not on main thread."""
        return _sig_mod.SIG_DFL

    try:
        _sig_mod.signal = _noop_signal              # type: ignore[assignment]
        log.info("Starting arm: %s", name)
        entry_fn()
    except Exception:
        log.exception("Arm %s crashed", name)
    finally:
        _sig_mod.signal = _orig                     # type: ignore[assignment]


# ── Helper: session config summary ───────────────────────────────────

_FORCE_FLAGS = [
    "TL_FORCE_INGEST_TICK",
    "TL_FORCE_SIGNAL_FIRE",
    "TL_FORCE_RISK_APPROVE",
    "TL_FORCE_EXEC_FILL",
    "TL_FORCE_EXIT_TRIM",
    "TL_FORCE_EXIT_FULL",
    "TL_FORCE_SCORECARD_UPDATE",
    "TL_FORCE_ATTRIB_OPEN",
    "TL_FORCE_ATTRIB_FILL",
    "TL_FORCE_ATTRIB_CLOSE",
    "TL_FORCE_EXIT_REGISTER",
]


def _log_session_config(settings) -> None:
    """Log a compact block of the active paper-session configuration."""
    env = os.environ.get
    mode = settings.trade_mode.value
    exec_enabled = env("EXECUTION_ENABLED", "false")
    backend = env("TRADE_LABS_EXECUTION_BACKEND", "SIM")
    armed = env("TRADE_LABS_ARMED", "0")
    heat_cap = env("TL_HEAT_CAP_ENABLED", "false")
    max_pos = env("TL_HEAT_MAX_OPEN_POS", "5")
    risk_usd = env("MAX_RISK_USD_PER_TRADE", "100")
    daily_loss = env("TL_KS_DAILY_LOSS_PCT", "0.03")
    ext_hours = env("ALLOW_EXTENDED_HOURS", "true")

    active_forces = [f for f in _FORCE_FLAGS if env(f, "").lower() in ("1", "true", "yes")]

    log.info("=" * 60)
    log.info("SESSION CONFIG SUMMARY")
    log.info("-" * 60)
    log.info("  trade_mode        = %s", mode)
    log.info("  execution_enabled = %s", exec_enabled)
    log.info("  exec_backend      = %s", backend)
    log.info("  armed             = %s", armed)
    log.info("  heat_cap_enabled  = %s", heat_cap)
    log.info("  max_open_pos      = %s", max_pos)
    log.info("  risk_per_trade    = $%s", risk_usd)
    log.info("  daily_loss_limit  = %s%%", float(daily_loss) * 100)
    log.info("  extended_hours    = %s", ext_hours)
    log.info("  first_paper_mode  = %s", _FIRST_PAPER)
    log.info("  cert_mode         = %s", _CERT_MODE)
    if active_forces:
        log.warning("  FORCE FLAGS ACTIVE: %s", active_forces)
        if _FIRST_PAPER:
            log.warning(
                "  ⚠ Force-path flags should be OFF for real paper sessions!"
            )
    else:
        log.info("  force_flags       = none (clean)")
    log.info("=" * 60)


def _log_startup_health(threads: list, bus) -> None:
    """Verify all arms are alive and config is paper-safe after launch."""
    time.sleep(0.5)  # brief settle
    alive = [t.name for t in threads if t.is_alive()]
    dead = [t.name for t in threads if not t.is_alive()]

    if dead:
        log.error("STARTUP HEALTH: arms DEAD on launch: %s", dead)
    else:
        log.info("STARTUP HEALTH: all %d arms alive %s", len(alive), alive)

    exec_en = os.environ.get("EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
    armed = os.environ.get("TRADE_LABS_ARMED", "0") not in ("0", "false", "no", "")
    backend = os.environ.get("TRADE_LABS_EXECUTION_BACKEND", "SIM")

    if exec_en or armed:
        log.warning(
            "STARTUP HEALTH: execution_enabled=%s armed=%s — orders MAY be placed",
            exec_en, armed,
        )
    else:
        log.info("STARTUP HEALTH: paper-safe (exec=OFF, armed=OFF, backend=%s)", backend)

    bus_stats = bus.get_stats() if hasattr(bus, "get_stats") else {}
    handler_count = bus_stats.get("handlers", 0)
    if handler_count == 0 and hasattr(bus, "_handlers"):
        with bus._lock:
            handler_count = sum(len(v) for v in bus._handlers.values())
    log.info("STARTUP HEALTH: bus handlers registered = %d", handler_count)


def _log_shutdown_summary(threads: list) -> None:
    """Log a concise end-of-session report with trade counts and PnL."""
    elapsed = time.time() - _start_ts if _start_ts else 0
    mins = elapsed / 60.0

    alive_names = [t.name for t in threads if t.is_alive()]
    dead_names = [t.name for t in threads if not t.is_alive()]

    log.info("=" * 60)
    log.info("SHUTDOWN SUMMARY  (session %.1f min)", mins)
    log.info("-" * 60)
    log.info("  arms_alive  = %s", alive_names or "none")
    log.info("  arms_dead   = %s", dead_names or "none")

    # Risk counters
    try:
        from src.arms import risk_main as _rm
        log.info(
            "  risk: drafts=%s blueprints=%s approved=%s rejected=%s",
            getattr(_rm, "_drafts", "?"),
            getattr(_rm, "_blueprints", "?"),
            getattr(_rm, "_approved", "?"),
            getattr(_rm, "_rejected", "?"),
        )
    except Exception:
        log.info("  risk: counters unavailable")

    # Execution counters
    try:
        from src.arms import execution_main as _em
        log.info(
            "  exec: blueprints_recv=%s orders_processed=%s",
            getattr(_em, "_blueprints_received", "?"),
            getattr(_em, "_orders_processed", "?"),
        )
    except Exception:
        log.info("  exec: counters unavailable")

    # Exit summary
    try:
        from src.risk.exit_intelligence import get_exit_summary
        es = get_exit_summary()
        log.info(
            "  exits: trims=%s full=%s time_stops=%s open=%s",
            es.get("trims_total", "?"), es.get("exits_total", "?"),
            es.get("time_stops_total", "?"), es.get("open_count", "?"),
        )
    except Exception:
        log.info("  exits: summary unavailable")

    # PnL Attribution
    try:
        from src.analysis.pnl_attribution import get_recent_attribution_snapshot
        pa = get_recent_attribution_snapshot()
        log.info(
            "  pnl: total=%s closed=%s realized=$%s unrealized=$%s win_rate=%s",
            pa.get("total_trades", "?"), pa.get("closed_trades", "?"),
            pa.get("realized_pnl", "?"), pa.get("unrealized_pnl", "?"),
            pa.get("win_rate", "?"),
        )
    except Exception:
        log.info("  pnl: attribution unavailable")

    # Scorecard
    try:
        from src.analysis.playbook_scorecard import get_scorecard_summary
        sc = get_scorecard_summary()
        log.info(
            "  scorecard: confidence=%s playbooks=%d",
            sc.get("overall_confidence", "?"),
            len(sc.get("playbooks", {})),
        )
    except Exception:
        log.info("  scorecard: summary unavailable")

    # Final verdict
    crashed = [n for n in dead_names if n.startswith("arm-")]
    verdict = "STABLE" if not crashed else "UNSTABLE"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("-" * 60)
    log.info("  final_verdict = %s  at %s", verdict, ts)
    log.info("=" * 60)


# ── Lazy arm imports (done after env is set) ─────────────────────────

_ingest = None
_signal = None
_risk = None
_execution = None
_monitor = None


def main() -> None:
    global _ingest, _signal, _risk, _execution, _monitor

    # Create and inject the shared bus BEFORE importing arm modules
    shared_bus = LocalBus()
    set_shared_bus(shared_bus)

    from src.config.settings import settings

    # Validate .env file on startup
    try:
        from src.utils.env_loader import validate_env
        validate_env()
    except Exception as exc:
        log.warning(".env validation skipped: %s", exc)

    _cert_label = "YES" if _CERT_MODE else "no"
    print(_BANNER.format(mode=settings.trade_mode.value, cert=_cert_label))

    if _CERT_MODE:
        log.info(
            "paper_cert_start mode=%s execution_enabled=%s cert_mode=CERTIFICATION",
            settings.trade_mode.value,
            os.environ.get("EXECUTION_ENABLED", "false"),
        )
        if os.environ.get("EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes"):
            log.critical(
                "SAFETY ABORT: EXECUTION_ENABLED=true during CERTIFICATION run. "
                "Certification must run with EXECUTION_ENABLED=false (paper only)."
            )
            sys.exit(1)

    # ── Paper session config summary ─────────────────────────────
    _log_session_config(settings)

    # Import arm modules (they call get_bus() internally via _connect_bus)
    from src.arms import ingest_main as _ingest_mod
    from src.arms import signal_main as _signal_mod
    from src.arms import risk_main as _risk_mod
    from src.arms import execution_main as _execution_mod
    from src.arms import monitor_main as _monitor_mod

    _ingest = _ingest_mod
    _signal = _signal_mod
    _risk = _risk_mod
    _execution = _execution_mod
    _monitor = _monitor_mod

    # Track heartbeats for watchdog
    def _track_heartbeat(hb) -> None:
        _last_heartbeat[hb.arm] = time.time()

    from src.bus.topics import HEARTBEAT as _HB_TOPIC
    from src.schemas.messages import Heartbeat as _HB_Msg
    shared_bus.subscribe(_HB_TOPIC, _track_heartbeat, msg_type=_HB_Msg)

    # Register shutdown handler (overrides per-arm handlers)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Launch each arm's main() in a daemon thread.
    # IMPORTANT: Subscribers (signal, risk, execution, monitor) must start
    # BEFORE the publisher (ingest) so no messages are lost.
    subscriber_arms = [
        ("signal", _signal_mod.main),
        ("risk", _risk_mod.main),
        ("execution", _execution_mod.main),
        ("monitor", _monitor_mod.main),
    ]
    publisher_arms = [
        ("ingest", _ingest_mod.main),
    ]

    threads: list[threading.Thread] = []

    # Phase 1: start all subscriber arms
    for name, fn in subscriber_arms:
        t = threading.Thread(target=_run_arm, args=(name, fn), daemon=True, name=f"arm-{name}")
        t.start()
        threads.append(t)
        time.sleep(0.15)

    # Give subscribers time to call _connect_bus() and register handlers
    log.info("Subscriber arms launched — waiting 1 s for subscriptions…")
    time.sleep(1.0)

    # Phase 2: start ingest (publisher) AFTER subscribers are ready
    for name, fn in publisher_arms:
        t = threading.Thread(target=_run_arm, args=(name, fn), daemon=True, name=f"arm-{name}")
        t.start()
        threads.append(t)
        time.sleep(0.15)

    # Log registered bus handlers for diagnostics
    if hasattr(shared_bus, '_handlers'):
        with shared_bus._lock:
            topics = list(shared_bus._handlers.keys())
            handler_count = sum(len(v) for v in shared_bus._handlers.values())
        log.info("Bus handlers: %d handlers on %d topics: %s", handler_count, len(topics), topics)

    log.info("All %d arms launched — waiting for Ctrl+C", len(threads))

    # ── Startup health checks ────────────────────────────────────
    _log_startup_health(threads, shared_bus)

    global _start_ts
    _start_ts = time.time()

    # Block main thread until shutdown, with periodic diagnostic summary
    _diag_interval = 10.0
    _last_diag = time.time()
    try:
        while _running:
            time.sleep(0.5)
            now = time.time()
            if now - _last_diag >= _diag_interval:
                _last_diag = now
                alive_names = [t.name for t in threads if t.is_alive()]
                dead_names = [t.name for t in threads if not t.is_alive()]
                bus_stats = shared_bus.get_stats() if hasattr(shared_bus, 'get_stats') else {}
                log.info(
                    "pipeline_diag  alive=%s  dead=%s  bus=%s",
                    alive_names, dead_names or "none",
                    {k: v for k, v in bus_stats.items() if v} if bus_stats else "n/a",
                )

                # ── Arm crash recovery ───────────────────────────
                if dead_names:
                    for t in threads:
                        if not t.is_alive() and t.name.startswith("arm-"):
                            arm_name = t.name.replace("arm-", "")
                            log.warning(
                                "ARM CRASH DETECTED: %s — attempting restart",
                                arm_name,
                            )
                            arm_fn_map = {
                                "ingest": _ingest.main if _ingest else None,
                                "signal": _signal.main if _signal else None,
                                "risk": _risk.main if _risk else None,
                                "execution": _execution.main if _execution else None,
                                "monitor": _monitor.main if _monitor else None,
                            }
                            fn = arm_fn_map.get(arm_name)
                            if fn is not None:
                                new_t = threading.Thread(
                                    target=_run_arm,
                                    args=(arm_name, fn),
                                    daemon=True,
                                    name=f"arm-{arm_name}",
                                )
                                new_t.start()
                                threads[threads.index(t)] = new_t
                                log.info(
                                    "ARM RESTARTED: %s (new thread id=%d)",
                                    arm_name,
                                    new_t.ident,
                                )
                            else:
                                log.error(
                                    "Cannot restart %s — module not loaded",
                                    arm_name,
                                )

                # ── Heartbeat watchdog ───────────────────────────
                _now_hb = time.time()
                for arm_name in ["ingest", "signal", "risk", "execution", "monitor"]:
                    last_hb = _last_heartbeat.get(arm_name, _start_ts)
                    gap = _now_hb - last_hb
                    if gap > _HEARTBEAT_TIMEOUT_S:
                        log.warning(
                            "WATCHDOG: arm %s has not sent a heartbeat in %.0fs (threshold=%.0fs)",
                            arm_name, gap, _HEARTBEAT_TIMEOUT_S,
                        )
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)

    # ── Shutdown summary ─────────────────────────────────────────
    _log_shutdown_summary(threads)

    # Give arms time to wind down (ingest may need up to 10+ s)
    log.info("Waiting for arm threads to exit…")
    for t in threads:
        t.join(timeout=20.0)

    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        log.warning("Arms still alive after timeout: %s", alive)

    log.info("Dev runner stopped.")


if __name__ == "__main__":
    main()
