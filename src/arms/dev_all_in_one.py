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

# ── Force local bus before any arm import ────────────────────────────
os.environ.setdefault("BUS_BACKEND", "local")

from src.bus.local_bus import LocalBus
from src.bus.bus_factory import set_shared_bus
from src.monitoring.logger import get_logger

log = get_logger("dev_runner")

_BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║         TRADE-LABS  ·  Dev All-in-One Runner             ║
║                                                          ║
║  Arms : ingest · signal · risk · execution · monitor     ║
║  Bus  : LocalBus (in-memory, no Redis)                   ║
║  Mode : {mode:<8s}                                       ║
║                                                          ║
║  Press Ctrl+C to stop                                    ║
╚══════════════════════════════════════════════════════════╝
"""

_running = True


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

    print(_BANNER.format(mode=settings.trade_mode.value))

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

    # Register shutdown handler (overrides per-arm handlers)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Launch each arm's main() in a daemon thread
    arms = [
        ("ingest", _ingest_mod.main),
        ("signal", _signal_mod.main),
        ("risk", _risk_mod.main),
        ("execution", _execution_mod.main),
        ("monitor", _monitor_mod.main),
    ]

    threads: list[threading.Thread] = []
    for name, fn in arms:
        t = threading.Thread(target=_run_arm, args=(name, fn), daemon=True, name=f"arm-{name}")
        t.start()
        threads.append(t)
        # Small stagger so log output is readable on startup
        time.sleep(0.2)

    log.info("All %d arms launched — waiting for Ctrl+C", len(threads))

    # Block main thread until shutdown
    try:
        while _running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)

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
