"""
SQLite Database Manager

High-level interface for all database operations.
Handles reads, writes, migrations, exports.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
import json

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from src.database.models import (
    create_database, get_session,
    Run, Trade, Signal, Position, DailyMetrics, PerformanceSummary
)


class TradeLabsDB:
    """Modern SQLite database manager."""
    
    def __init__(self, db_path: str = "data/trade_labs.db"):
        self.db_path = db_path
        create_database(db_path)
    
    def get_session(self) -> Session:
        """Get a database session."""
        return get_session(self.db_path)
    
    # ========================
    # RUN OPERATIONS
    # ========================
    
    def record_run(
        self,
        run_id: str,
        backend: str,
        armed: bool,
        num_scanned: int,
        num_executed: int,
        num_successful: int,
        details: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Record a pipeline execution run."""
        session = self.get_session()
        
        run = Run(
            run_id=run_id,
            timestamp=datetime.utcnow(),
            backend=backend,
            armed=armed,
            num_candidates_scanned=num_scanned,
            num_candidates_executed=num_executed,
            num_successful=num_successful,
            details=json.dumps(details or {}),
        )
        
        session.add(run)
        session.commit()
        
        # Return dict before closing session
        result = {
            "id": run.id,
            "run_id": run.run_id,
            "timestamp": run.timestamp.isoformat(),
            "backend": run.backend,
            "armed": run.armed,
        }
        
        session.close()
        return result
    
    def get_runs(self, limit: int = 100) -> List[Run]:
        """Get recent pipeline runs."""
        session = self.get_session()
        runs = session.query(Run).order_by(Run.timestamp.desc()).limit(limit).all()
        session.close()
        return runs
    
    def get_run_by_id(self, run_id: str) -> Optional[Run]:
        """Get a specific run by ID."""
        session = self.get_session()
        run = session.query(Run).filter(Run.run_id == run_id).first()
        session.close()
        return run
    
    # ========================
    # TRADE OPERATIONS
    # ========================
    
    def record_trade(
        self,
        run_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        entry_timestamp: str,
        quantity: int,
        stop_loss: float,
        parent_order_id: Optional[int] = None,
        stop_order_id: Optional[int] = None,
    ) -> Trade:
        """Record an executed trade."""
        session = self.get_session()
        
        trade = Trade(
            run_id_fk=run_id,
            symbol=symbol,
            side=side.upper(),
            entry_price=entry_price,
            entry_timestamp=datetime.fromisoformat(entry_timestamp) if isinstance(entry_timestamp, str) else entry_timestamp,
            quantity=quantity,
            stop_loss=stop_loss,
            parent_order_id=parent_order_id,
            stop_order_id=stop_order_id,
            status="OPEN",
        )
        
        session.add(trade)
        session.commit()
        session.close()
        
        return trade
    
    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_timestamp: Optional[str] = None,
    ) -> Optional[Trade]:
        """Mark a trade as closed and calculate P&L."""
        session = self.get_session()
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        
        if not trade:
            session.close()
            return None
        
        exit_time = datetime.fromisoformat(exit_timestamp) if isinstance(exit_timestamp, str) else (datetime.utcnow() if exit_timestamp is None else exit_timestamp)
        duration_secs = (exit_time - trade.entry_timestamp).total_seconds()
        
        # Calculate P&L
        if trade.side.upper() == "BUY":
            realized_pnl = (exit_price - trade.entry_price) * trade.quantity
            realized_pnl_pct = ((exit_price / trade.entry_price) - 1.0) * 100.0
        else:  # SELL
            realized_pnl = (trade.entry_price - exit_price) * trade.quantity
            realized_pnl_pct = ((trade.entry_price / exit_price) - 1.0) * 100.0
        
        trade.exit_price = exit_price
        trade.exit_timestamp = exit_time
        trade.status = "CLOSED"
        trade.realized_pnl = round(realized_pnl, 2)
        trade.realized_pnl_pct = round(realized_pnl_pct, 4)
        trade.duration_seconds = int(duration_secs)
        
        session.commit()
        session.close()
        
        return trade
    
    def get_trades(
        self,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Trade]:
        """Query trades with filters."""
        session = self.get_session()
        query = session.query(Trade)
        
        if symbol:
            query = query.filter(Trade.symbol == symbol)
        if status:
            query = query.filter(Trade.status == status)
        
        trades = query.order_by(Trade.entry_timestamp.desc()).limit(limit).all()
        session.close()
        
        return trades
    
    def get_trades_for_date(self, date_str: str) -> List[Trade]:
        """Get all trades for a specific date."""
        session = self.get_session()
        
        start_dt = datetime.strptime(date_str, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=1)
        
        trades = session.query(Trade).filter(
            and_(
                Trade.entry_timestamp >= start_dt,
                Trade.entry_timestamp < end_dt,
                Trade.status == "CLOSED"
            )
        ).all()
        
        session.close()
        return trades
    
    # ========================
    # SIGNAL OPERATIONS
    # ========================
    
    def record_signal(
        self,
        run_id: str,
        symbol: str,
        score: float,
        ranking: int,
        parameters: Dict[str, Any] = None,
    ) -> Signal:
        """Record a scan signal."""
        session = self.get_session()
        
        signal = Signal(
            run_id_fk=run_id,
            symbol=symbol,
            score=score,
            ranking=ranking,
            parameters=json.dumps(parameters or {}),
        )
        
        session.add(signal)
        session.commit()
        session.close()
        
        return signal
    
    # ========================
    # POSITION OPERATIONS
    # ========================
    
    def update_position(
        self,
        symbol: str,
        quantity: int,
        avg_cost: float,
        current_price: float,
        stop_loss: float,
        entry_timestamp: str,
        entry_order_id: int,
    ) -> Position:
        """Update or create a position."""
        session = self.get_session()
        
        position = session.query(Position).filter(Position.symbol == symbol).first()
        
        if not position:
            position = Position(symbol=symbol)
            session.add(position)
        
        position.quantity = quantity
        position.avg_cost = avg_cost
        position.current_price = current_price
        position.stop_loss = stop_loss
        position.entry_timestamp = datetime.fromisoformat(entry_timestamp) if isinstance(entry_timestamp, str) else entry_timestamp
        position.entry_order_id = entry_order_id
        position.timestamp = datetime.utcnow()
        
        # Calculate P&L
        if quantity != 0:
            unrealized = (current_price - avg_cost) * quantity
            unrealized_pct = ((current_price / avg_cost) - 1.0) * 100.0 if avg_cost != 0 else 0.0
            position.unrealized_pnl = round(unrealized, 2)
            position.unrealized_pnl_pct = round(unrealized_pct, 2)
        
        position.reconciliation_status = "PENDING"
        
        session.commit()
        session.close()
        
        return position
    
    def get_open_positions(self) -> List[Position]:
        """Get all open positions."""
        session = self.get_session()
        positions = session.query(Position).filter(Position.quantity > 0).all()
        session.close()
        return positions
    
    def close_position(self, symbol: str):
        """Close a position (set quantity to 0)."""
        session = self.get_session()
        position = session.query(Position).filter(Position.symbol == symbol).first()
        
        if position:
            position.quantity = 0
            position.unrealized_pnl = 0.0
            position.unrealized_pnl_pct = 0.0
            session.commit()
        
        session.close()
    
    # ========================
    # METRICS OPERATIONS
    # ========================
    
    def record_daily_metrics(
        self,
        date: str,
        daily_pnl: float,
        cumulative_pnl: float,
        num_trades: int,
        num_wins: int,
        num_losses: int,
        win_rate_pct: float,
        sharpe_ratio: float,
        sortino_ratio: float,
        calmar_ratio: float,
        max_drawdown_pct: float,
        volatility_pct: float,
        profit_factor: float,
    ) -> DailyMetrics:
        """Record daily metrics."""
        session = self.get_session()
        
        metrics = DailyMetrics(
            date=date,
            daily_pnl=daily_pnl,
            cumulative_pnl=cumulative_pnl,
            num_trades=num_trades,
            num_wins=num_wins,
            num_losses=num_losses,
            win_rate_pct=win_rate_pct,
            max_drawdown_pct=max_drawdown_pct,
            volatility_pct=volatility_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            calmar_ratio=calmar_ratio,
            profit_factor=profit_factor,
        )
        
        session.add(metrics)
        session.commit()
        session.close()
        
        return metrics
    
    def update_performance_summary(
        self,
        total_trades: int,
        total_pnl: float,
        win_rate_pct: float,
        sharpe_ratio: float,
        sortino_ratio: float,
        max_drawdown_pct: float,
        profit_factor: float,
        recovery_factor: float,
    ) -> PerformanceSummary:
        """Update overall performance summary."""
        session = self.get_session()
        
        summary = session.query(PerformanceSummary).first()
        if not summary:
            summary = PerformanceSummary()
            session.add(summary)
        
        summary.total_trades = total_trades
        summary.total_pnl = total_pnl
        summary.win_rate_pct = win_rate_pct
        summary.sharpe_ratio = sharpe_ratio
        summary.sortino_ratio = sortino_ratio
        summary.max_drawdown_pct = max_drawdown_pct
        summary.profit_factor = profit_factor
        summary.recovery_factor = recovery_factor
        summary.updated_at = datetime.utcnow()
        
        # Set first/last trade dates from trades
        trades = session.query(Trade).filter(Trade.status == "CLOSED").all()
        if trades:
            first_trade = min(trades, key=lambda t: t.entry_timestamp)
            last_trade = max(trades, key=lambda t: t.exit_timestamp)
            summary.first_trade_date = first_trade.entry_timestamp
            summary.last_trade_date = last_trade.exit_timestamp
        
        session.commit()
        session.close()
        
        return summary
    
    # ========================
    # STATISTICS
    # ========================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get overall trading statistics."""
        session = self.get_session()
        
        closed_trades = session.query(Trade).filter(Trade.status == "CLOSED").all()
        open_trades = session.query(Trade).filter(Trade.status == "OPEN").all()
        
        total_pnl = sum(t.realized_pnl or 0 for t in closed_trades)
        wins = sum(1 for t in closed_trades if (t.realized_pnl or 0) > 0)
        losses = sum(1 for t in closed_trades if (t.realized_pnl or 0) < 0)
        
        stats = {
            "total_trades": len(closed_trades),
            "open_trades": len(open_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(closed_trades) * 100) if closed_trades else 0, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_trade_pnl": round(total_pnl / len(closed_trades), 2) if closed_trades else 0,
        }
        
        session.close()
        return stats
    
    # ========================
    # EXPORT
    # ========================
    
    def export_trades_to_json(self, filename: str = "trades_export.json"):
        """Export all trades to JSON."""
        import json
        
        trades = self.get_trades()
        data = []
        
        for trade in trades:
            data.append({
                "symbol": trade.symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "entry_timestamp": trade.entry_timestamp.isoformat() if trade.entry_timestamp else None,
                "exit_price": trade.exit_price,
                "exit_timestamp": trade.exit_timestamp.isoformat() if trade.exit_timestamp else None,
                "stop_loss": trade.stop_loss,
                "realized_pnl": trade.realized_pnl,
                "realized_pnl_pct": trade.realized_pnl_pct,
                "status": trade.status,
                "duration_seconds": trade.duration_seconds,
            })
        
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"✓ Exported {len(data)} trades to {filename}")
        return filename
