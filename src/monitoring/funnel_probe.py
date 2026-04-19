from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _ProbeCounters:
    total_candidates_seen: int = 0
    signal_session_policy_rejects: int = 0
    execution_session_policy_rejects: int = 0


class FunnelProbe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = _ProbeCounters()

    def record_candidate_seen(self, count: int = 1) -> None:
        with self._lock:
            self._counters.total_candidates_seen += max(0, int(count))

    def record_signal_session_reject(self) -> None:
        with self._lock:
            self._counters.signal_session_policy_rejects += 1

    def record_execution_session_reject(self) -> None:
        with self._lock:
            self._counters.execution_session_policy_rejects += 1

    def snapshot(self) -> _ProbeCounters:
        with self._lock:
            return _ProbeCounters(
                total_candidates_seen=self._counters.total_candidates_seen,
                signal_session_policy_rejects=self._counters.signal_session_policy_rejects,
                execution_session_policy_rejects=self._counters.execution_session_policy_rejects,
            )


funnel_probe = FunnelProbe()
