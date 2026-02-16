"""
Structured logging infrastructure for Trade Labs.

Provides centralized logging to:
- Console (INFO and above)
- File (DEBUG and above for full audit trail)
- Structured JSON for parsing

Usage:
    from src.utils.log_manager import get_logger
    logger = get_logger(__name__)
    logger.info("message")
"""

import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """Format log records as JSON for easy parsing."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        
        # Include exception traceback if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Include any extra fields passed via logger.info(..., extra={...})
        if hasattr(record, "context"):
            log_data["context"] = record.context
        
        return json.dumps(log_data)


def setup_logging(
    name: str,
    log_dir: str = "logs",
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Setup a logger with both console and file handlers.
    
    Args:
        name: Logger name (typically __name__)
        log_dir: Directory to store log files
        console_level: Minimum level for console output
        file_level: Minimum level for file output
    
    Returns:
        Configured logger instance
    """
    
    # Ensure log directory exists
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True, parents=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Capture everything, handlers filter
    
    # Avoid duplicate handlers if logger already configured
    if logger.handlers:
        return logger
    
    # Console handler - clean, readable format
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # File handler - JSON format for parsing
    log_file = log_path / f"{name.replace('.', '_')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(file_level)
    file_handler.setFormatter(JSONFormatter())
    logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get or create a logger for a module.
    
    Usage in any module:
        from src.utils.log_manager import get_logger
        logger = get_logger(__name__)
    """
    return setup_logging(name)


class PipelineLogger:
    """
    High-level pipeline event logger.
    Tracks execution flow with structured events.
    """
    
    _instance = None
    
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.logger = get_logger("pipeline")
        self.events = []
    
    @staticmethod
    def get_logger(name: str = "pipeline") -> "PipelineLogger":
        """Get or create the pipeline logger."""
        if PipelineLogger._instance is None:
            PipelineLogger._instance = PipelineLogger(name)
        return PipelineLogger._instance
        
    def event(self, event_type: str, data: Dict[str, Any], level: str = "INFO"):
        """Log a pipeline event with structured data."""
        record = {
            "run_id": self.run_id,
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data
        }
        
        self.events.append(record)
        
        message = f"[{event_type}] {json.dumps(data)}"
        if level == "ERROR":
            self.logger.error(message)
        elif level == "WARNING":
            self.logger.warning(message)
        else:
            self.logger.info(message)
    
    def scan_started(self, run_id: str):
        self.event("scan_started", {"run_id": run_id})
    
    def scan_completed(self, found: int):
        self.event("scan_completed", {"symbols_found": found})
    
    def scoring_started(self, count: int):
        self.event("scoring_started", {"candidate_count": count})
    
    def candidate_scored(self, symbol: str, score: float, reason: str):
        self.event("candidate_scored", {
            "symbol": symbol,
            "score": score,
            "reason": reason
        })
    
    def execution_started(self, symbol: str, side: str):
        self.event("execution_started", {"symbol": symbol, "side": side})
    
    def execution_completed(
        self,
        run_id: str,
        symbol: str,
        shares: int,
        entry_price: float,
        stop_loss: float,
        order_id: Optional[int],
        ok: bool,
        reason: str = None,
    ):
        level = "INFO" if ok else "WARNING"
        data = {
            "run_id": run_id,
            "symbol": symbol,
            "shares": shares,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "order_id": order_id,
            "success": ok,
        }
        if reason:
            data["reason"] = reason
        self.event("execution_completed", data, level=level)
    
    def execution_failed(self, symbol: str, error: str):
        self.event("execution_failed", {
            "symbol": symbol,
            "error": error
        }, level="ERROR")
    
    def pipeline_completed(self, run_id: str, executed: int, successful: int):
        self.event("pipeline_completed", {
            "run_id": run_id,
            "candidates_executed": executed,
            "successful_executions": successful,
        })
            "event_count": len(self.events),
            "events": self.events
        }
