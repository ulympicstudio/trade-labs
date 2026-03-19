#!/usr/bin/env python3
"""
U.T.S. Dashboard HTTP Server

Serves dashboard/index.html and syncs logs/live_status.json from the
trade-labs root every 2 seconds.  Falls back to sample_live_status.json
when the live file does not yet exist.

Usage:
    python3 serve_dashboard.py              # default port 8080
    python3 serve_dashboard.py --port 9090  # custom port
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).resolve().parent
TRADE_LABS_ROOT = DASHBOARD_DIR.parent          # ~/trade-labs
LIVE_SOURCE = TRADE_LABS_ROOT / "logs" / "live_status.json"
LIVE_LOCAL = DASHBOARD_DIR / "live_status.json"
SAMPLE_FILE = DASHBOARD_DIR / "sample_live_status.json"

SYNC_INTERVAL_S = 2.0


# ── File sync thread ────────────────────────────────────────────────

def _sync_loop(stop_event: threading.Event) -> None:
    """Copy logs/live_status.json → dashboard/live_status.json every 2 s."""
    while not stop_event.is_set():
        try:
            if LIVE_SOURCE.exists():
                shutil.copy2(str(LIVE_SOURCE), str(LIVE_LOCAL))
            elif SAMPLE_FILE.exists() and not LIVE_LOCAL.exists():
                shutil.copy2(str(SAMPLE_FILE), str(LIVE_LOCAL))
        except Exception:
            pass  # best-effort; never crash the sync thread
        stop_event.wait(SYNC_INTERVAL_S)


# ── HTTP handler ─────────────────────────────────────────────────────

class _Handler(SimpleHTTPRequestHandler):
    """Serve files from the dashboard directory with CORS and no-cache
    headers for live_status.json."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def end_headers(self):
        # CORS — allow any origin (local network iPad access)
        self.send_header("Access-Control-Allow-Origin", "*")
        # No-cache for JSON data files
        if self.path and self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        """Suppress noisy per-request logs; only log errors."""
        if args and isinstance(args[0], str) and args[0].startswith("GET"):
            return  # silence GET spam
        super().log_message(format, *args)


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="U.T.S. Dashboard Server")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    args = parser.parse_args()

    # Seed with sample if no live data yet
    if not LIVE_LOCAL.exists() and SAMPLE_FILE.exists():
        shutil.copy2(str(SAMPLE_FILE), str(LIVE_LOCAL))

    # Start sync thread
    stop = threading.Event()
    sync_thread = threading.Thread(target=_sync_loop, args=(stop,), daemon=True)
    sync_thread.start()

    # Resolve LAN IP for iPad access hint
    lan_ip = _get_lan_ip()

    print(f"{'=' * 56}")
    print(f"  U.T.S. Web Dashboard")
    print(f"  Local:   http://localhost:{args.port}")
    if lan_ip:
        print(f"  Network: http://{lan_ip}:{args.port}")
    print(f"  Syncing: {LIVE_SOURCE}")
    print(f"{'=' * 56}")

    server = HTTPServer(("0.0.0.0", args.port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        print("\nDashboard server stopped.")


def _get_lan_ip() -> str | None:
    """Best-effort LAN IP detection."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == "__main__":
    main()
