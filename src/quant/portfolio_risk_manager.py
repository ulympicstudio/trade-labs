"""
Portfolio Risk Manager
Manages hundreds of simultaneous swing trades with portfolio-level risk controls.
Handles position sizing, exposure limits, correlation management, and order coordination.
"""

import logging
from dataclasses import dataclass
from typing import List, Dict, Optional
from datetime import datetime

from src.quant.quant_scorer import QuantScore


logger = logging.getLogger(__name__)


@dataclass
class PortfolioPosition:
    """Represents an open position in the portfolio."""
    symbol: str
    direction: str  # LONG or SHORT
    entry_price: float
    current_price: float
    quantity: int
    stop_loss: float
    profit_target: float
    entry_timestamp: str
    
    # Risk metrics
    position_size: float  # In dollars
    risk_amount: float  # Max loss if stop hit
    risk_pct: float  # % of portfolio at risk
    
    # P&L
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass
class PortfolioMetrics:
    """Real-time portfolio analytics."""
    timestamp: str
    
    # Capital
    total_capital: float
    deployed_capital: float
    available_capital: float
    capital_utilization_pct: float
    
    # Positions
    num_positions: int
    num_long: int
    num_short: int
    
    # Risk
    total_risk_amount: float  # Sum of all position risks
    total_risk_pct: float  # % of portfolio at risk
    max_drawdown_current: float
    
    # P&L
    unrealized_pnl: float
    unrealized_pnl_pct: float
    
    # Concentration
    largest_position_pct: float
    avg_position_size_pct: float
    
    # Correlation (placeholder)
    avg_correlation: Optional[float] = None


class PortfolioRiskManager:
    """
    Manages portfolio-level risk for high-frequency swing trading.
    Handles 100+ simultaneous positions with sophisticated controls.
    """
    
    def __init__(self, total_capital: float, 
                 max_positions: int = 100,
                 max_risk_per_trade_pct: float = 1.0,
                 max_total_risk_pct: float = 20.0,
                 max_position_size_pct: float = 5.0,
                 max_capital_utilization_pct: float = 90.0,
                 max_sector_exposure_pct: float = 25.0):
        """
        Initialize portfolio risk manager.
        
        Args:
            total_capital: Total trading capital
            max_positions: Maximum number of simultaneous positions
            max_risk_per_trade_pct: Max % of capital to risk per trade
            max_total_risk_pct: Max % of capital at risk across all trades
            max_position_size_pct: Max % of capital per position
            max_capital_utilization_pct: Max % of capital deployed
            max_sector_exposure_pct: Max % exposure to single sector
        """
        self.total_capital = total_capital
        self.max_positions = max_positions
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_total_risk_pct = max_total_risk_pct
        self.max_position_size_pct = max_position_size_pct
        self.max_capital_utilization_pct = max_capital_utilization_pct
        self.max_sector_exposure_pct = max_sector_exposure_pct
        
        self.positions: List[PortfolioPosition] = []
        self.pending_orders: List[QuantScore] = []
        
        logger.info(f"PortfolioRiskManager initialized: ${total_capital:,.2f} capital, "
                   f"max {max_positions} positions, {max_risk_per_trade_pct}% risk/trade")
    
    def calculate_position_size(self, score: QuantScore, 
                               max_risk_dollars: float) -> tuple[int, float]:
        """
        Calculate optimal position size based on risk parameters.
        
        Uses the entry and stop loss to determine how many shares to buy
        while staying within risk limits.
        
        Returns: (quantity, position_size_dollars)
        """
        entry = score.suggested_entry
        stop = score.suggested_stop
        
        # Risk per share
        risk_per_share = abs(entry - stop)
        
        if risk_per_share == 0:
            logger.warning(f"{score.symbol}: Zero risk per share, cannot size position")
            return 0, 0.0
        
        # Calculate quantity based on max risk
        quantity = int(max_risk_dollars / risk_per_share)
        
        # Position size in dollars
        position_size = quantity * entry
        
        # Check position size limits
        max_position_size = self.total_capital * (self.max_position_size_pct / 100)
        if position_size > max_position_size:
            # Reduce quantity to meet position size limit
            quantity = int(max_position_size / entry)
            position_size = quantity * entry
        
        logger.debug(f"{score.symbol}: Size calculated - {quantity} shares, "
                    f"${position_size:,.2f}, risk ${max_risk_dollars:,.2f}")
        
        return quantity, position_size
    
    def can_add_position(self, score: QuantScore, quantity: int, 
                        position_size: float) -> tuple[bool, str]:
        """
        Check if a new position can be added within risk constraints.
        
        Returns: (can_add, reason)
        """
        # Check position count
        if len(self.positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"
        
        # Check capital utilization
        deployed = sum(p.position_size for p in self.positions)
        new_deployed = deployed + position_size
        utilization = (new_deployed / self.total_capital) * 100
        
        if utilization > self.max_capital_utilization_pct:
            return False, f"Capital utilization would exceed limit ({utilization:.1f}% > {self.max_capital_utilization_pct}%)"
        
        # Check total risk
        total_risk = sum(p.risk_amount for p in self.positions)
        new_risk = quantity * abs(score.suggested_entry - score.suggested_stop)
        new_total_risk = total_risk + new_risk
        risk_pct = (new_total_risk / self.total_capital) * 100
        
        if risk_pct > self.max_total_risk_pct:
            return False, f"Total portfolio risk would exceed limit ({risk_pct:.1f}% > {self.max_total_risk_pct}%)"
        
        # Check for duplicate symbol
        if any(p.symbol == score.symbol for p in self.positions):
            return False, f"Already have position in {score.symbol}"
        
        return True, "OK"
    
    def evaluate_opportunity(self, score: QuantScore) -> Optional[Dict]:
        """
        Evaluate if an opportunity should be traded and with what size.
        
        Returns None if trade should not be taken, otherwise returns dict with:
        - quantity, position_size, risk_amount, etc.
        """
        # Calculate max risk for this trade
        max_risk_dollars = self.total_capital * (self.max_risk_per_trade_pct / 100)
        
        # Calculate position size
        quantity, position_size = self.calculate_position_size(score, max_risk_dollars)
        
        if quantity == 0:
            logger.info(f"{score.symbol}: Position size too small, skipping")
            return None
        
        # Check if position can be added
        can_add, reason = self.can_add_position(score, quantity, position_size)
        
        if not can_add:
            logger.info(f"{score.symbol}: Cannot add position - {reason}")
            return None
        
        # Calculate risk metrics
        risk_amount = quantity * abs(score.suggested_entry - score.suggested_stop)
        risk_pct = (risk_amount / self.total_capital) * 100
        
        position_info = {
            "symbol": score.symbol,
            "direction": score.direction,
            "quantity": quantity,
            "entry_price": score.suggested_entry,
            "stop_loss": score.suggested_stop,
            "profit_target": score.suggested_target,
            "position_size": position_size,
            "risk_amount": risk_amount,
            "risk_pct": risk_pct,
            "score": score.total_score,
            "confidence": score.confidence,
            "expected_return_pct": score.expected_return_pct
        }
        
        logger.info(f"{score.symbol}: ✓ Approved - {quantity} shares @ ${score.suggested_entry:.2f}, "
                   f"risk ${risk_amount:,.2f} ({risk_pct:.2f}%)")
        
        return position_info
    
    def prioritize_opportunities(self, scores: List[QuantScore]) -> List[Dict]:
        """
        Prioritize and size all opportunities within portfolio constraints.
        
        Returns list of approved positions with sizing information.
        """
        approved_positions = []
        
        # Sort by score * confidence (best opportunities first)
        sorted_scores = sorted(
            scores,
            key=lambda s: s.total_score * s.confidence,
            reverse=True
        )
        
        logger.info(f"Evaluating {len(sorted_scores)} opportunities...")
        
        for score in sorted_scores:
            position_info = self.evaluate_opportunity(score)
            
            if position_info:
                approved_positions.append(position_info)
                
                # Simulate adding position (for remaining evaluations)
                self.positions.append(PortfolioPosition(
                    symbol=position_info["symbol"],
                    direction=position_info["direction"],
                    entry_price=position_info["entry_price"],
                    current_price=position_info["entry_price"],
                    quantity=position_info["quantity"],
                    stop_loss=position_info["stop_loss"],
                    profit_target=position_info["profit_target"],
                    entry_timestamp=datetime.now().isoformat(),
                    position_size=position_info["position_size"],
                    risk_amount=position_info["risk_amount"],
                    risk_pct=position_info["risk_pct"],
                    unrealized_pnl=0.0,
                    unrealized_pnl_pct=0.0
                ))
            
            # Stop if max positions reached
            if len(approved_positions) >= self.max_positions:
                logger.info(f"Max positions ({self.max_positions}) reached")
                break
        
        logger.info(f"Approved {len(approved_positions)} positions for execution")
        
        return approved_positions
    
    def update_position_price(self, symbol: str, current_price: float):
        """Update current price for a position and recalculate P&L."""
        for position in self.positions:
            if position.symbol == symbol:
                position.current_price = current_price
                
                if position.direction == "LONG":
                    position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
                else:
                    position.unrealized_pnl = (position.entry_price - current_price) * position.quantity
                
                position.unrealized_pnl_pct = (position.unrealized_pnl / position.position_size) * 100
                break
    
    def close_position(self, symbol: str, exit_price: float) -> Optional[Dict]:
        """Close a position and return P&L details."""
        for i, position in enumerate(self.positions):
            if position.symbol == symbol:
                # Calculate final P&L
                if position.direction == "LONG":
                    realized_pnl = (exit_price - position.entry_price) * position.quantity
                else:
                    realized_pnl = (position.entry_price - exit_price) * position.quantity
                
                realized_pnl_pct = (realized_pnl / position.position_size) * 100
                
                close_info = {
                    "symbol": symbol,
                    "direction": position.direction,
                    "entry_price": position.entry_price,
                    "exit_price": exit_price,
                    "quantity": position.quantity,
                    "realized_pnl": realized_pnl,
                    "realized_pnl_pct": realized_pnl_pct,
                    "position_size": position.position_size,
                    "entry_timestamp": position.entry_timestamp,
                    "exit_timestamp": datetime.now().isoformat()
                }
                
                # Remove position
                self.positions.pop(i)
                
                logger.info(f"{symbol}: Position closed @ ${exit_price:.2f}, "
                           f"P&L ${realized_pnl:+,.2f} ({realized_pnl_pct:+.2f}%)")
                
                return close_info
        
        return None
    
    def get_portfolio_metrics(self) -> PortfolioMetrics:
        """Calculate current portfolio metrics."""
        timestamp = datetime.now().isoformat()
        
        deployed_capital = sum(p.position_size for p in self.positions)
        available_capital = self.total_capital - deployed_capital
        capital_utilization = (deployed_capital / self.total_capital) * 100
        
        num_long = sum(1 for p in self.positions if p.direction == "LONG")
        num_short = sum(1 for p in self.positions if p.direction == "SHORT")
        
        total_risk = sum(p.risk_amount for p in self.positions)
        total_risk_pct = (total_risk / self.total_capital) * 100 if self.total_capital > 0 else 0
        
        total_unrealized_pnl = sum(p.unrealized_pnl for p in self.positions)
        total_unrealized_pnl_pct = (total_unrealized_pnl / deployed_capital) * 100 if deployed_capital > 0 else 0
        
        largest_position = max([p.position_size for p in self.positions], default=0)
        largest_position_pct = (largest_position / self.total_capital) * 100
        
        avg_position_size = deployed_capital / len(self.positions) if self.positions else 0
        avg_position_size_pct = (avg_position_size / self.total_capital) * 100
        
        return PortfolioMetrics(
            timestamp=timestamp,
            total_capital=self.total_capital,
            deployed_capital=deployed_capital,
            available_capital=available_capital,
            capital_utilization_pct=capital_utilization,
            num_positions=len(self.positions),
            num_long=num_long,
            num_short=num_short,
            total_risk_amount=total_risk,
            total_risk_pct=total_risk_pct,
            max_drawdown_current=0.0,  # TODO: Calculate from equity curve
            unrealized_pnl=total_unrealized_pnl,
            unrealized_pnl_pct=total_unrealized_pnl_pct,
            largest_position_pct=largest_position_pct,
            avg_position_size_pct=avg_position_size_pct
        )
    
    def display_portfolio_status(self):
        """Display current portfolio status."""
        metrics = self.get_portfolio_metrics()
        
        print(f"\n{'='*80}")
        print(f"PORTFOLIO STATUS - {metrics.timestamp}")
        print(f"{'='*80}")
        print(f"\n--- CAPITAL ---")
        print(f"Total Capital:        ${metrics.total_capital:>12,.2f}")
        print(f"Deployed:             ${metrics.deployed_capital:>12,.2f}  ({metrics.capital_utilization_pct:.1f}%)")
        print(f"Available:            ${metrics.available_capital:>12,.2f}")
        print(f"\n--- POSITIONS ---")
        print(f"Open Positions:       {metrics.num_positions:>12}  (max {self.max_positions})")
        print(f"Long:                 {metrics.num_long:>12}")
        print(f"Short:                {metrics.num_short:>12}")
        print(f"\n--- RISK ---")
        print(f"Total at Risk:        ${metrics.total_risk_amount:>12,.2f}  ({metrics.total_risk_pct:.1f}%)")
        print(f"Max Total Risk:       {self.max_total_risk_pct:>12.1f}%")
        print(f"Risk per Trade:       {self.max_risk_per_trade_pct:>12.1f}%")
        print(f"\n--- P&L ---")
        print(f"Unrealized P&L:       ${metrics.unrealized_pnl:>+12,.2f}  ({metrics.unrealized_pnl_pct:+.2f}%)")
        print(f"\n--- CONCENTRATION ---")
        print(f"Largest Position:     {metrics.largest_position_pct:>12.1f}%  (max {self.max_position_size_pct:.1f}%)")
        print(f"Avg Position Size:    {metrics.avg_position_size_pct:>12.1f}%")
        print(f"{'='*80}\n")
    
    def display_open_positions(self, top_n: int = 20):
        """Display open positions."""
        if not self.positions:
            print("\nNo open positions\n")
            return
        
        print(f"\n{'='*100}")
        print(f"OPEN POSITIONS ({len(self.positions)})")
        print(f"{'='*100}\n")
        
        print(f"{'Symbol':<8}{'Dir':<6}{'Qty':<8}{'Entry':<10}{'Current':<10}"
              f"{'Stop':<10}{'Target':<10}{'P&L $':<12}{'P&L %':<10}")
        print("-" * 100)
        
        # Sort by unrealized P&L
        sorted_positions = sorted(self.positions, key=lambda p: p.unrealized_pnl, reverse=True)
        
        for position in sorted_positions[:top_n]:
            print(f"{position.symbol:<8}{position.direction:<6}{position.quantity:<8}"
                  f"${position.entry_price:<9.2f}${position.current_price:<9.2f}"
                  f"${position.stop_loss:<9.2f}${position.profit_target:<9.2f}"
                  f"${position.unrealized_pnl:>+10,.2f}  {position.unrealized_pnl_pct:>+7.2f}%")
        
        print(f"\n{'='*100}\n")
