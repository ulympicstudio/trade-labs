import os
import time
import logging

from ib_insync import IB

log = logging.getLogger("ib_session")

_ib_instance: IB | None = None


def get_ib() -> IB:
    """
    Return a shared, persistent IB connection (singleton).
    Creates and connects on first call; reuses on subsequent calls.
    Thread-safe for read — do not call from multiple asyncio loops.
    """
    global _ib_instance

    if _ib_instance is not None and _ib_instance.isConnected():
        return _ib_instance

    host      = os.getenv("IB_HOST",      "127.0.0.1")
    port      = int(os.getenv("IB_PORT",      "7497"))
    # Signal arm uses dedicated client ID to avoid collision with execution arm
    client_id = int(os.getenv("TL_SIGNAL_IB_CLIENT_ID", os.getenv("IB_CLIENT_ID", "2")))

    retries      = 3
    delay        = 1.0
    last_error   = None

    for attempt in range(1, retries + 1):
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=10)
            if ib.isConnected():
                _ib_instance = ib
                log.info("ib_session_connected host=%s port=%d client_id=%d",
                         host, port, client_id)
                return _ib_instance
            last_error = RuntimeError("Connected but isConnected() returned False")
        except Exception as exc:
            last_error = exc
            log.warning("ib_session_connect_attempt attempt=%d/%d err=%s",
                        attempt, retries, exc)
        finally:
            if not ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass
        if attempt < retries:
            time.sleep(delay)
            delay *= 2

    raise ConnectionError(
        f"IB connection failed after {retries} attempts "
        f"(host={host} port={port} client_id={client_id})"
    ) from last_error


def disconnect_ib() -> None:
    """Graceful shutdown — call from UTS teardown."""
    global _ib_instance
    if _ib_instance is not None:
        try:
            _ib_instance.disconnect()
            log.info("ib_session_disconnected")
        except Exception:
            pass
        _ib_instance = None
