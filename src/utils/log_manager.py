import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# Module-level singleton so get_logger() works from anywhere after setup
_logger_instance = None  # type: PipelineLogger | None


def setup_logging(name: str = "trade_labs", log_dir: str = "logs") -> str:
    """
    Create log directory and initialise a module-level PipelineLogger.

    Compatible with orchestrator calls like:
      setup_logging("trade_labs_orchestrator", log_dir="logs/pipeline")
    """
    final_dir = os.path.join(log_dir, name)
    os.makedirs(final_dir, exist_ok=True)

    global _logger_instance
    _logger_instance = PipelineLogger(name=name, log_dir=log_dir)
    return final_dir


@dataclass
class PipelineLogger:
    """
    Minimal logger used by orchestrator / pipeline.
    Stores events in memory and writes jsonl to disk.
    """

    name: str = "pipeline"
    log_dir: str = "logs"
    run_id: str = field(
        default_factory=lambda: datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    )
    events: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        self.base_dir = os.path.join(self.log_dir, self.name)
        os.makedirs(self.base_dir, exist_ok=True)
        self.filepath = os.path.join(
            self.base_dir, f"events_{self.run_id}.jsonl"
        )

    # ---- class-level accessor ----
    @staticmethod
    def get_logger(name: str = "pipeline"):
        """Return the module-level logger created by setup_logging().
        
        Accepts an optional *name* argument so callers like
        ``PipelineLogger.get_logger("pipeline_orchestrator")``
        work without error.  The name is only used when no
        logger has been initialised yet.
        """
        global _logger_instance
        if _logger_instance is None:
            _logger_instance = PipelineLogger(name=name)
        return _logger_instance

    # ---- logging helpers ----
    def log(
        self,
        event: str,
        payload: Optional[Dict[str, Any]] = None,
        level: str = "INFO",
    ):
        rec = {
            "ts": datetime.utcnow().isoformat(),
            "level": level,
            "event": event,
            "payload": payload or {},
        }
        self.events.append(rec)
        print(f"[{rec['level']}] {rec['event']}")
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # ---- pipeline lifecycle helpers ----
    def scan_started(self, run_id: str):
        self.log("scan_started", {"run_id": run_id})

    def execution_completed(self, run_id: str, symbol: str, shares: int,
                            entry_price: float, stop_loss: float,
                            order_id: Optional[int] = None,
                            ok: bool = True, reason: str = ""):
        self.log("execution_completed", {
            "run_id": run_id, "symbol": symbol, "shares": shares,
            "entry_price": entry_price, "stop_loss": stop_loss,
            "order_id": order_id, "ok": ok, "reason": reason,
        })

    def pipeline_completed(self, run_id: str, executed: int, successful: int):
        self.log("pipeline_completed", {
            "run_id": run_id, "executed": executed, "successful": successful,
        })

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_count": len(self.events),
            "log_file": self.filepath,
        }
