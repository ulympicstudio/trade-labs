"""IB client-ID isolation tests.

Verifies per-arm connection tracking, duplicate detection, and teardown.

Run with:  pytest tests/test_ib_client_isolation.py -v
"""

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import src.broker.ib_session as sess


# ── Helpers ──────────────────────────────────────────────────────────

def _reset_arm_state():
    """Clear per-arm tracking between tests."""
    with sess._arm_lock:
        sess._arm_connections.clear()


def _mock_ib_connected():
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connect.return_value = None
    return ib


def _mock_ib_class(ib_instance):
    """Return a patcher for ib_insync.IB that returns the given instance."""
    return patch("src.broker.ib_session.IB", return_value=ib_instance)


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════

class TestArmConnectionTracking:

    def setup_method(self):
        _reset_arm_state()

    def teardown_method(self):
        _reset_arm_state()

    def test_connect_ib_for_arm_registers_connection(self):
        """connect_ib_for_arm stores (IB, client_id) in _arm_connections."""
        mock_ib = _mock_ib_connected()
        with _mock_ib_class(mock_ib):
            result = sess.connect_ib_for_arm("execution", client_id=10)
        assert result is mock_ib
        assert "execution" in sess._arm_connections
        assert sess._arm_connections["execution"] == (mock_ib, 10)

    def test_duplicate_arm_reuses_connection(self):
        """Connecting the same arm name twice returns existing connection."""
        mock_ib = _mock_ib_connected()
        with _mock_ib_class(mock_ib):
            first = sess.connect_ib_for_arm("signal", client_id=20)
        with _mock_ib_class(_mock_ib_connected()):
            second = sess.connect_ib_for_arm("signal", client_id=20)
        assert first is second

    def test_duplicate_client_id_raises(self):
        """Two different arms with the same client_id → RuntimeError."""
        mock_ib = _mock_ib_connected()
        with _mock_ib_class(mock_ib):
            sess.connect_ib_for_arm("execution", client_id=30)
        with _mock_ib_class(_mock_ib_connected()):
            with pytest.raises(RuntimeError, match="already in use"):
                sess.connect_ib_for_arm("monitor", client_id=30)

    def test_disconnect_arm(self):
        """disconnect_arm removes the arm and calls ib.disconnect()."""
        mock_ib = _mock_ib_connected()
        with _mock_ib_class(mock_ib):
            sess.connect_ib_for_arm("ingest", client_id=40)
        sess.disconnect_arm("ingest")
        assert "ingest" not in sess._arm_connections
        mock_ib.disconnect.assert_called_once()

    def test_disconnect_arm_nonexistent_is_noop(self):
        """Disconnecting a non-existent arm should not raise."""
        sess.disconnect_arm("nonexistent")  # no error

    def test_get_all_arm_client_ids(self):
        """get_all_arm_client_ids returns a dict of arm→client_id."""
        mock_ib1 = _mock_ib_connected()
        mock_ib2 = _mock_ib_connected()
        with _mock_ib_class(mock_ib1):
            sess.connect_ib_for_arm("execution", client_id=50)
        with _mock_ib_class(mock_ib2):
            sess.connect_ib_for_arm("monitor", client_id=51)
        ids = sess.get_all_arm_client_ids()
        assert ids == {"execution": 50, "monitor": 51}

    def test_disconnect_all_arms(self):
        """disconnect_all_arms tears down every tracked connection."""
        mock_ib1 = _mock_ib_connected()
        mock_ib2 = _mock_ib_connected()
        with _mock_ib_class(mock_ib1):
            sess.connect_ib_for_arm("execution", client_id=60)
        with _mock_ib_class(mock_ib2):
            sess.connect_ib_for_arm("signal", client_id=61)
        sess.disconnect_all_arms()
        assert len(sess._arm_connections) == 0
        mock_ib1.disconnect.assert_called_once()
        mock_ib2.disconnect.assert_called_once()


class TestExecutionMainConnectBroker:
    """Verify execution_main.connect_broker() can use connect_ib_for_arm."""

    def setup_method(self):
        _reset_arm_state()

    def teardown_method(self):
        _reset_arm_state()

    def test_connect_broker_no_arg_uses_arm_connection(self):
        """connect_broker() with no arg delegates to connect_ib_for_arm."""
        mock_ib = _mock_ib_connected()
        with _mock_ib_class(mock_ib), \
             patch.dict(os.environ, {"TL_EXECUTION_IB_CLIENT_ID": "70"}):
            from src.arms.execution_main import connect_broker, _ib
            connect_broker()
        # _ib should now be set (it's a module global)
        import src.arms.execution_main as em
        assert em._ib is mock_ib

    def test_connect_broker_with_arg_uses_provided_ib(self):
        """connect_broker(ib) still works for legacy callers."""
        mock_ib = MagicMock()
        from src.arms.execution_main import connect_broker
        connect_broker(mock_ib)
        import src.arms.execution_main as em
        assert em._ib is mock_ib
