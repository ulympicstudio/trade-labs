import asyncio
import os
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from ib_insync import IB

log = logging.getLogger("ib_session")


# ── Broker state telemetry ───────────────────────────────────────────

@dataclass
class BrokerState:
    """Observable broker connection state, updated on every connect/disconnect."""
    connected: bool = False
    client_id: int = 0
    host: str = ""
    port: int = 0
    readonly: bool = False
    last_connect_ts: float = 0.0
    last_error: str = ""
    last_error_ts: float = 0.0
    connect_count: int = 0
    error_count: int = 0

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "client_id": self.client_id,
            "host": self.host,
            "port": self.port,
            "readonly": self.readonly,
            "last_connect_ts": self.last_connect_ts,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "connect_count": self.connect_count,
            "error_count": self.error_count,
        }


_broker_state = BrokerState()


def get_broker_state() -> BrokerState:
    """Return the singleton broker state (read-only snapshot for diagnostics)."""
    return _broker_state


# ── Asyncio loop safety ─────────────────────────────────────────────

def _ensure_event_loop() -> None:
    """Ensure an asyncio event loop exists on the current thread.

    ib_insync internally calls ``asyncio.get_event_loop()`` which raises
    ``RuntimeError`` in non-main threads that have no loop.  This helper
    creates and installs one when needed, preventing the ``connectAsync
    was never awaited`` and ``no current event loop`` warnings.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


# ── Singleton per-process connection ─────────────────────────────────

_ib_instance: IB | None = None
_ib_client_id_used: int | None = None  # guard against client ID reuse

# ── Per-arm dedicated connections ────────────────────────────────────
_arm_connections: dict[str, tuple[IB, int]] = {}   # arm_name -> (IB, client_id)
_arm_lock = threading.Lock()


def get_ib(
    *,
    client_id_env: str = "TL_SIGNAL_IB_CLIENT_ID",
    client_id_fallback: str = "IB_CLIENT_ID",
    default_client_id: int = 2,
    readonly: bool = False,
) -> IB:
    """Return a shared, persistent IB connection (singleton).

    Creates and connects on first call; reuses on subsequent calls.
    Ensures an asyncio event loop exists on the calling thread before
    connecting, so this is safe to call from arm threads/processes.

    Parameters
    ----------
    client_id_env : str
        Primary env var for the client ID.
    client_id_fallback : str
        Fallback env var when *client_id_env* is unset.
    default_client_id : int
        Hard default when neither env var is set.
    readonly : bool
        If True, connect in read-only mode (monitor arm).
    """
    global _ib_instance, _ib_client_id_used

    if _ib_instance is not None and _ib_instance.isConnected():
        return _ib_instance

    _ensure_event_loop()

    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "7497"))
    client_id = int(os.getenv(client_id_env, os.getenv(client_id_fallback, str(default_client_id))))

    # Guard: detect client ID collision across different event loops
    if _ib_client_id_used is not None and _ib_client_id_used == client_id and _ib_instance is not None:
        try:
            existing_loop = getattr(_ib_instance, "_loop", None)
            current_loop = asyncio.get_event_loop()
            if existing_loop is not None and existing_loop is not current_loop:
                raise RuntimeError(
                    f"Client ID {client_id} already in use on a different event loop. "
                    f"Set a unique IB_CLIENT_ID per arm to avoid disconnection."
                )
        except RuntimeError as e:
            if "already in use" in str(e):
                raise

    retries = 3
    delay = 1.0
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=10, readonly=readonly)
            if ib.isConnected():
                _ib_instance = ib
                _ib_client_id_used = client_id
                _broker_state.connected = True
                _broker_state.client_id = client_id
                _broker_state.host = host
                _broker_state.port = port
                _broker_state.readonly = readonly
                _broker_state.last_connect_ts = time.time()
                _broker_state.connect_count += 1
                log.info(
                    "ib_session_connected host=%s port=%d client_id=%d readonly=%s",
                    host, port, client_id, readonly,
                )
                return _ib_instance
            last_error = RuntimeError("Connected but isConnected() returned False")
        except Exception as exc:
            last_error = exc
            _broker_state.last_error = str(exc)
            _broker_state.last_error_ts = time.time()
            _broker_state.error_count += 1
            log.warning(
                "ib_session_connect_attempt attempt=%d/%d err=%s",
                attempt, retries, exc,
            )
        finally:
            if not ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass
        if attempt < retries:
            time.sleep(delay)
            delay *= 2

    _broker_state.connected = False
    raise ConnectionError(
        f"IB connection failed after {retries} attempts "
        f"(host={host} port={port} client_id={client_id})"
    ) from last_error


def connect_ib_for_arm(
    arm_name: str,
    client_id: int | None = None,
    *,
    readonly: bool = False,
) -> IB:
    """Dedicated IB connection for a named arm.

    Each arm gets its own IB instance with a unique client ID.
    If *client_id* is None, it is read from ``TL_{ARM}_IB_CLIENT_ID``
    (uppercased arm name), falling back to a deterministic hash.

    Connections are tracked in ``_arm_connections`` so they can be
    torn down individually or all at once.
    """
    _ensure_event_loop()

    if client_id is None:
        env_key = f"TL_{arm_name.upper()}_IB_CLIENT_ID"
        client_id = int(os.getenv(env_key, str(abs(hash(arm_name)) % 90 + 10)))

    with _arm_lock:
        # Reject duplicate arm names that are still connected
        if arm_name in _arm_connections:
            existing_ib, existing_cid = _arm_connections[arm_name]
            if existing_ib.isConnected():
                log.warning(
                    "connect_ib_for_arm arm=%s already connected (client_id=%d), reusing",
                    arm_name, existing_cid,
                )
                return existing_ib

        # Reject duplicate client IDs across arms
        for other_arm, (other_ib, other_cid) in _arm_connections.items():
            if other_cid == client_id and other_ib.isConnected():
                raise RuntimeError(
                    f"Client ID {client_id} already in use by arm '{other_arm}'. "
                    f"Set a unique TL_{arm_name.upper()}_IB_CLIENT_ID."
                )

    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "7497"))

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=readonly)
        if ib.isConnected():
            with _arm_lock:
                _arm_connections[arm_name] = (ib, client_id)
            _broker_state.connected = True
            _broker_state.client_id = client_id
            _broker_state.host = host
            _broker_state.port = port
            _broker_state.readonly = readonly
            _broker_state.last_connect_ts = time.time()
            _broker_state.connect_count += 1
            log.info(
                "ib_session_connected arm=%s host=%s port=%d client_id=%d readonly=%s",
                arm_name, host, port, client_id, readonly,
            )
            return ib
        raise RuntimeError("Connected but isConnected() returned False")
    except Exception as exc:
        _broker_state.last_error = str(exc)
        _broker_state.last_error_ts = time.time()
        _broker_state.error_count += 1
        _broker_state.connected = False
        try:
            ib.disconnect()
        except Exception:
            pass
        raise


def disconnect_ib() -> None:
    """Graceful shutdown — disconnect singleton and all arm connections."""
    global _ib_instance
    if _ib_instance is not None:
        try:
            _ib_instance.disconnect()
            log.info("ib_session_disconnected")
        except Exception:
            pass
        _ib_instance = None
    _broker_state.connected = False


def disconnect_arm(arm_name: str) -> None:
    """Disconnect a single arm's dedicated IB connection."""
    with _arm_lock:
        entry = _arm_connections.pop(arm_name, None)
    if entry is not None:
        ib, cid = entry
        try:
            ib.disconnect()
            log.info("ib_session_arm_disconnected arm=%s client_id=%d", arm_name, cid)
        except Exception:
            pass


def get_all_arm_client_ids() -> dict[str, int]:
    """Return {arm_name: client_id} for all tracked arm connections."""
    with _arm_lock:
        return {name: cid for name, (_, cid) in _arm_connections.items()}


def disconnect_all_arms() -> None:
    """Disconnect every tracked arm connection. Call from UTS teardown."""
    with _arm_lock:
        arms = list(_arm_connections.items())
        _arm_connections.clear()
    for arm_name, (ib, cid) in arms:
        try:
            ib.disconnect()
            log.info("ib_session_arm_disconnected arm=%s client_id=%d", arm_name, cid)
        except Exception:
            pass
