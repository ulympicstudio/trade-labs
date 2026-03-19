"""Quick verification script — run after code changes."""
import sys

def main():
    from src.arms import ingest_main, signal_main, risk_main, execution_main, monitor_main
    from src.monitoring.logger import _SafeStreamHandler
    from src.utils.env_loader import validate_env

    ok = True
    for name, mod in [
        ("ingest", ingest_main),
        ("signal", signal_main),
        ("risk", risk_main),
        ("execution", execution_main),
        ("monitor", monitor_main),
    ]:
        has_stop = hasattr(mod, "_stop_event")
        has_run = hasattr(mod, "_running")
        status = "OK" if (has_stop and has_run) else "FAIL"
        print(f"  {name:12s}  _stop_event={has_stop}  _running={has_run}  [{status}]")
        if status == "FAIL":
            ok = False

    # Signal consensus helpers
    for attr in ("_recent_consensus_count", "_COOLDOWN_CONSENSUS_S", "_CONFIDENCE_CONSENSUS_BOOST"):
        has = hasattr(signal_main, attr)
        print(f"  signal.{attr:40s}  {'OK' if has else 'FAIL'}")
        if not has:
            ok = False

    # Monitor iMessage
    for attr in ("_maybe_alert_consensus", "_send_imessage"):
        has = hasattr(monitor_main, attr)
        print(f"  monitor.{attr:40s}  {'OK' if has else 'FAIL'}")
        if not has:
            ok = False

    # Logger
    print(f"  logger._SafeStreamHandler bases={_SafeStreamHandler.__bases__}")

    # Env loader
    bad = validate_env()
    print(f"  env_loader: {len(bad)} invalid lines")

    print()
    if ok:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()
