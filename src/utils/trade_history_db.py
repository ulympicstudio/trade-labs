"""
Trade History Persistence

Stores executed trades and pipeline runs to local storage for:
- Audit trail
- Performance analysis
- PnL calculation
- Backtesting

Storage format: JSON for simplicity, can migrate to DB later.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

from src.contracts.trade_intent import TradeIntent
from src.execution.orders import OrderResult


class TradeHistoryDB:
    """Local JSON-based trade history database."""
    
    def __init__(self, db_dir: str = "data/trade_history"):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        
        self.runs_file = self.db_dir / "runs.json"
        self.trades_file = self.db_dir / "trades.json"
        self.candidates_file = self.db_dir / "candidates.json"
    
    def _load_json(self, file_path: Path) -> List[Dict[str, Any]]:
        """Load JSON file, return empty list if not found."""
        if file_path.exists():
            with open(file_path) as f:
                return json.load(f)
        return []
    
    def _save_json(self, file_path: Path, data: List[Dict[str, Any]]):
        """Atomically save JSON file."""
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    
    def record_pipeline_run(
        self,
        run_id: str,
        backend: str,
        armed: bool,
        num_candidates_scanned: int,
        num_candidates_executed: int,
        num_successful: int,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record a pipeline execution run."""
        
        run_record = {
            "run_id": run_id,
            "timestamp": datetime.utcnow().isoformat(),
            "backend": backend,
            "armed": armed,
            "stats": {
                "scanned": num_candidates_scanned,
                "executed": num_candidates_executed,
                "successful": num_successful,
                "failed": num_candidates_executed - num_successful,
            },
            "details": details,
        }
        
        runs = self._load_json(self.runs_file)
        runs.append(run_record)
        self._save_json(self.runs_file, runs)
        
        return run_record
    
    def record_trade(
        self,
        run_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        order_result: OrderResult,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an executed trade."""
        
        trade_record = {
            "run_id": run_id,
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "order_id": order_result.parent_order_id,
            "stop_order_id": order_result.stop_order_id,
            "order_success": order_result.ok,
            "entry_timestamp": timestamp or datetime.utcnow().isoformat(),
            "order_message": order_result.message,
            "status": "OPEN",  # OPEN, CLOSED, CANCELLED
            "exit_price": None,
            "exit_timestamp": None,
            "pnl": None,
            "pnl_percent": None,
        }
        
        trades = self._load_json(self.trades_file)
        trades.append(trade_record)
        self._save_json(self.trades_file, trades)
        
        return trade_record

    def record_candidate(
        self,
        run_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        rationale: str,
        backend: str,
        armed: bool,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a suggested trade candidate (non-executed)."""

        candidate_record = {
            "run_id": run_id,
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "rationale": rationale,
            "backend": backend,
            "armed": armed,
            "timestamp": timestamp or datetime.utcnow().isoformat(),
            "status": "SUGGESTED",
        }

        candidates = self._load_json(self.candidates_file)
        candidates.append(candidate_record)
        self._save_json(self.candidates_file, candidates)

        return candidate_record
    
    def close_trade(
        self,
        order_id: int,
        exit_price: float,
        exit_timestamp: Optional[str] = None,
    ):
        """Mark a trade as closed and calculate PnL."""
        
        trades = self._load_json(self.trades_file)
        
        for trade in trades:
            if trade["order_id"] == order_id:
                exit_ts = exit_timestamp or datetime.utcnow().isoformat()
                
                # Calculate P&L
                if trade["side"].upper() == "BUY":
                    pnl = (exit_price - trade["entry_price"]) * trade["quantity"]
                    pnl_pct = ((exit_price / trade["entry_price"]) - 1.0) * 100.0
                else:  # SELL
                    pnl = (trade["entry_price"] - exit_price) * trade["quantity"]
                    pnl_pct = ((trade["entry_price"] / exit_price) - 1.0) * 100.0
                
                trade.update({
                    "status": "CLOSED",
                    "exit_price": exit_price,
                    "exit_timestamp": exit_ts,
                    "pnl": round(pnl, 2),
                    "pnl_percent": round(pnl_pct, 4),
                })
                
                self._save_json(self.trades_file, trades)
                return trade
        
        return None
    
    def get_run_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent pipeline runs."""
        runs = self._load_json(self.runs_file)
        return runs[-limit:]
    
    def get_trade_history(self, symbol: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get trade history, optionally filtered."""
        trades = self._load_json(self.trades_file)
        
        if symbol:
            trades = [t for t in trades if t["symbol"] == symbol]
        
        if status:
            trades = [t for t in trades if t["status"] == status]
        
        return trades

    def get_candidate_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent suggested candidates."""
        candidates = self._load_json(self.candidates_file)
        return candidates[-limit:]
    
    def get_daily_summary(self, date: Optional[str] = None) -> Dict[str, Any]:
        """Get PnL summary for a date."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        
        trades = self._load_json(self.trades_file)
        
        # Filter to trades from this date
        daily_trades = [
            t for t in trades
            if t["entry_timestamp"].startswith(date) and t["status"] == "CLOSED"
        ]
        
        if not daily_trades:
            return {
                "date": date,
                "trades": 0,
                "total_pnl": 0.0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
            }
        
        total_pnl = sum(t.get("pnl", 0.0) for t in daily_trades)
        wins = sum(1 for t in daily_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in daily_trades if t.get("pnl", 0) < 0)
        win_rate = (wins / len(daily_trades) * 100.0) if daily_trades else 0.0
        
        return {
            "date": date,
            "trades": len(daily_trades),
            "total_pnl": round(total_pnl, 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get overall trading statistics."""
        runs = self._load_json(self.runs_file)
        trades = self._load_json(self.trades_file)
        
        closed_trades = [t for t in trades if t["status"] == "CLOSED"]
        
        total_pnl = sum(t.get("pnl", 0.0) for t in closed_trades)
        wins = sum(1 for t in closed_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in closed_trades if t.get("pnl", 0) < 0)
        
        return {
            "pipeline_runs": len(runs),
            "total_trades": len(trades),
            "closed_trades": len(closed_trades),
            "open_trades": len([t for t in trades if t["status"] == "OPEN"]),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(closed_trades) * 100.0) if closed_trades else 0.0, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_trade_pnl": round(total_pnl / len(closed_trades), 2) if closed_trades else 0.0,
        }
