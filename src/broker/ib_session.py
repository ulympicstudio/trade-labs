import os
import time

from ib_insync import IB


def get_ib() -> IB:
    """
    Return a connected IB instance.

    Uses environment variables with defaults:
    - IB_HOST (default: 127.0.0.1)
    - IB_PORT (default: 7497)
    - IB_CLIENT_ID (default: 999)

    Retries connection up to 3 times with simple backoff.
    """
    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "7497"))
    client_id = int(os.getenv("IB_CLIENT_ID", "999"))

    retries = 3
    delay_seconds = 1.0
    last_error = None

    for attempt in range(1, retries + 1):
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=10)
            if ib.isConnected():
                return ib
            last_error = RuntimeError("Connection attempt returned disconnected state")
        except Exception as exc:
            last_error = exc
        finally:
            if not ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

        if attempt < retries:
            time.sleep(delay_seconds)
            delay_seconds *= 2

    raise ConnectionError(
        f"Unable to connect to Interactive Brokers after {retries} attempts "
        f"(host={host}, port={port}, client_id={client_id})"
    ) from last_error
