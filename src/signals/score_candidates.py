# Backwards-compatible alias for older imports/tests.
# Supports both call styles:
#   score_candidates(candidates, top_n=5)
#   score_candidates(ib, candidates, top_n=5)
def score_candidates(*args, **kwargs):
    # If called as score_candidates(candidates, ...) then args[0] is scan_results
    # If called as score_candidates(ib, candidates, ...) then args[0] is IB and args[1] is scan_results
    if len(args) == 0:
        raise TypeError("score_candidates requires at least scan_results (or ib, scan_results)")

    # Detect IB-like first arg by attribute presence
    first = args[0]
    has_ib_methods = hasattr(first, "isConnected") and hasattr(first, "reqMktData")

    if has_ib_methods:
        # New style: (ib, scan_results, ...)
        return score_scan_results(*args, **kwargs)

    # Old style: (scan_results, ...)
    scan_results = args[0]
    rest = args[1:]

    # score_scan_results requires an IB instance.
    # To keep older tests working, create/connect an IB session lazily.
    from src.broker.ib_session import get_ib

    ib = get_ib()
    try:
        return score_scan_results(ib, scan_results, *rest, **kwargs)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass