"""
SQLite Database Models

Define database schema using SQLAlchemy ORM.
Supports trades, runs, signals, positions, metrics.
"""

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, Session
from datetime import datetime

Base = declarative_base()


class Run(Base):
    """Pipeline execution record."""
    
    __tablename__ = "runs"
    
    id = Column(Integer, primary_key=True)
    run_id = Column(String(50), unique=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    backend = Column(String(20))  # SIM, IB
    armed = Column(Boolean, default=False)
    
    # Statistics
    num_candidates_scanned = Column(Integer, default=0)
    num_candidates_executed = Column(Integer, default=0)
    num_successful = Column(Integer, default=0)
    
    # Metrics calculated afterward
    daily_pnl = Column(Float, nullable=True)  # Set after run complete
    
    # JSON details (flexible storage)
    details = Column(Text)  # JSON string
    
    # Relationships
    trades = relationship("Trade", back_populates="run")
    signals = relationship("Signal", back_populates="run")
    
    def __repr__(self):
        return f"<Run {self.run_id} [{self.timestamp}]>"


class Trade(Base):
    """Individual executed trade."""
    
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True)
    run_id_fk = Column(String(50), ForeignKey("runs.run_id"))
    
    # Trade identifiers
    symbol = Column(String(20), index=True)
    side = Column(String(10))  # BUY, SELL
    
    # Pricing
    entry_price = Column(Float)
    entry_timestamp = Column(DateTime, index=True)
    exit_price = Column(Float, nullable=True)
    exit_timestamp = Column(DateTime, nullable=True, index=True)
    
    # Quantity & Risk
    quantity = Column(Integer)
    stop_loss = Column(Float)
    
    # Order IDs
    parent_order_id = Column(Integer, nullable=True)
    stop_order_id = Column(Integer, nullable=True)
    
    # Status & P&L
    status = Column(String(20), default="OPEN")  # OPEN, CLOSED, CANCELLED
    realized_pnl = Column(Float, nullable=True)
    realized_pnl_pct = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)  # If still open
    
    # Trade duration
    duration_seconds = Column(Integer, nullable=True)
    
    # Relationships
    run = relationship("Run", back_populates="trades")
    
    def __repr__(self):
        return f"<Trade {self.symbol} {self.side} {self.quantity}@{self.entry_price} [{self.status}]>"


class Signal(Base):
    """Scan signal / candidate."""
    
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True)
    run_id_fk = Column(String(50), ForeignKey("runs.run_id"))
    
    # Signal info
    symbol = Column(String(20), index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Scoring
    score = Column(Float)
    ranking = Column(Integer)  # 1, 2, 3, ...
    
    # Parameters used
    parameters = Column(Text)  # JSON string
    
    # Relationships
    run = relationship("Run", back_populates="signals")
    
    def __repr__(self):
        return f"<Signal {self.symbol} score={self.score} rank={self.ranking}>"


class Position(Base):
    """Current open position."""
    
    __tablename__ = "positions"
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    # Position details
    quantity = Column(Integer)
    avg_cost = Column(Float)  # Entry price
    current_price = Column(Float)
    
    # Risk management
    stop_loss = Column(Float)
    
    # P&L tracking
    unrealized_pnl = Column(Float)
    unrealized_pnl_pct = Column(Float)
    
    # Reconciliation
    reconciliation_status = Column(String(20), default="PENDING")  # OK, MATCHED, MISMATCH
    
    # Entry details
    entry_timestamp = Column(DateTime)
    entry_order_id = Column(Integer)
    
    def __repr__(self):
        return f"<Position {self.symbol} qty={self.quantity} pnl=${self.unrealized_pnl}>"


class DailyMetrics(Base):
    """Daily aggregated metrics."""
    
    __tablename__ = "daily_metrics"
    
    id = Column(Integer, primary_key=True)
    date = Column(String(10), unique=True, index=True)  # YYYY-MM-DD
    calculated_at = Column(DateTime, default=datetime.utcnow)
    
    # P&L
    daily_pnl = Column(Float)
    cumulative_pnl = Column(Float)
    
    # Trade counts
    num_trades = Column(Integer)
    num_wins = Column(Integer)
    num_losses = Column(Integer)
    win_rate_pct = Column(Float)
    
    # Risk metrics
    max_drawdown_pct = Column(Float)
    volatility_pct = Column(Float)
    
    # Ratios
    sharpe_ratio = Column(Float)
    sortino_ratio = Column(Float)
    calmar_ratio = Column(Float)
    profit_factor = Column(Float)
    
    def __repr__(self):
        return f"<DailyMetrics {self.date} pnl=${self.daily_pnl} win_rate={self.win_rate_pct}%>"


class PerformanceSummary(Base):
    """Overall performance summary (updated daily)."""
    
    __tablename__ = "performance_summary"
    
    id = Column(Integer, primary_key=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    
    # Lifetime stats
    total_trades = Column(Integer)
    total_pnl = Column(Float)
    win_rate_pct = Column(Float)
    
    # Risk
    max_drawdown_pct = Column(Float)
    sharpe_ratio = Column(Float)
    sortino_ratio = Column(Float)
    
    # Efficiency
    profit_factor = Column(Float)
    recovery_factor = Column(Float)
    
    # Timing
    first_trade_date = Column(DateTime)
    last_trade_date = Column(DateTime)
    
    def __repr__(self):
        return f"<PerfSummary trades={self.total_trades} pnl=${self.total_pnl}>"


def create_database(db_path: str = "data/trade_labs.db"):
    """Create database and all tables."""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    print(f"✓ Database created/verified: {db_path}")
    return engine


def get_session(db_path: str = "data/trade_labs.db") -> Session:
    """Get a database session."""
    engine = create_engine(f"sqlite:///{db_path}")
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
