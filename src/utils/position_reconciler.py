"""
Position Reconciliation

Reconciles open positions from trade history against actual positions in IB:
- Fetch all open positions from IB
- Fetch expected positions from trade history
- Compare and identify discrepancies
- Calculate real vs expected P&L
- Flag positions for manual review

Ensures our records match reality.
"""

from typing import Dict, List, Any, Optional
from datetime import datetime

from ib_insync import IB

from src.data.ib_market_data import connect_ib
from src.utils.trade_history_db import TradeHistoryDB


class PositionReconciler:
    """Reconcile trade records against actual positions."""
    
    def __init__(self, db: Optional[TradeHistoryDB] = None):
        self.db = db or TradeHistoryDB("data/trade_history")
    
    def get_open_positions_ib(self, ib: IB) -> Dict[str, Dict[str, Any]]:
        """Fetch all open positions from IB."""
        positions = {}
        
        try:
            for position in ib.positions():
                symbol = position.contract.symbol
                qty = position.position
                
                # Get current market price
                ticker = ib.ticker(position.contract)
                ib.sleep(0.01)  # Small delay to let ticker populate
                
                current_price = ticker.last if ticker.last > 0 else ticker.midpoint()
                
                positions[symbol] = {
                    "symbol": symbol,
                    "quantity": qty,
                    "current_price": current_price,
                    "contract": position.contract,
                    "account": position.account,
                    "source": "IB",
                }
        
        except Exception as e:
            print(f"Error fetching positions from IB: {str(e)}")
        
        return positions
    
    def get_open_positions_trade_history(self) -> Dict[str, Dict[str, Any]]:
        """Get expected positions from unexited trades."""
        positions = {}
        
        trades = self.db.get_trade_history(status="OPEN")
        
        for trade in trades:
            symbol = trade["symbol"]
            
            if symbol not in positions:
                positions[symbol] = {
                    "symbol": symbol,
                    "quantity": 0,
                    "cost_basis": 0.0,
                    "entry_price": 0.0,
                    "stop_loss": 0.0,
                    "trades": [],
                    "source": "TradeHistory",
                }
            
            # Add to aggregate position
            qty = trade.get("quantity", 0)
            price = trade.get("entry_price", 0)
            
            positions[symbol]["quantity"] += qty if trade["side"] == "BUY" else -qty
            positions[symbol]["cost_basis"] += qty * price
            positions[symbol]["trades"].append(trade)
            
            # Track stop loss (most conservative)
            if trade.get("stop_loss", 0) > 0:
                if positions[symbol]["stop_loss"] == 0:
                    positions[symbol]["stop_loss"] = trade["stop_loss"]
                else:
                    positions[symbol]["stop_loss"] = min(
                        positions[symbol]["stop_loss"],
                        trade["stop_loss"]
                    )
        
        return positions
    
    def reconcile(self, ib: IB) -> Dict[str, Any]:
        """Compare IB positions to trade history."""
        ib_positions = self.get_open_positions_ib(ib)
        history_positions = self.get_open_positions_trade_history()
        
        # Gather all symbols
        all_symbols = set(ib_positions.keys()) | set(history_positions.keys())
        
        reconciliation = {
            "timestamp": datetime.utcnow().isoformat(),
            "matches": [],
            "ib_only": [],      # In IB but not in our history
            "history_only": [],  # In history but not in IB
            "quantity_mismatch": [],  # Different quantities
            "status": "OK",
        }
        
        for symbol in sorted(all_symbols):
            ib_pos = ib_positions.get(symbol)
            hist_pos = history_positions.get(symbol)
            
            if ib_pos and hist_pos:
                # Both exist - check if they match
                ib_qty = ib_pos["quantity"]
                hist_qty = hist_pos["quantity"]
                
                if ib_qty == hist_qty:
                    # Match found - calculate current P&L
                    if hist_qty != 0:
                        current_price = ib_pos["current_price"]
                        cost_basis = hist_pos["cost_basis"]
                        current_value = current_price * ib_qty
                        unrealized_pnl = current_value - cost_basis
                        pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis != 0 else 0
                        
                        reconciliation["matches"].append({
                            "symbol": symbol,
                            "quantity": ib_qty,
                            "entry_avg": round(cost_basis / ib_qty, 2),
                            "current_price": round(current_price, 2),
                            "unrealized_pnl": round(unrealized_pnl, 2),
                            "unrealized_pnl_percent": round(pnl_pct, 2),
                            "stop_loss": hist_pos.get("stop_loss", 0),
                        })
                else:
                    # Quantity mismatch
                    reconciliation["quantity_mismatch"].append({
                        "symbol": symbol,
                        "ib_qty": ib_qty,
                        "history_qty": hist_qty,
                        "discrepancy": ib_qty - hist_qty,
                        "ib_price": round(ib_pos["current_price"], 2),
                    })
                    reconciliation["status"] = "DISCREPANCY"
            
            elif ib_pos:
                # In IB only - might be a stale position
                reconciliation["ib_only"].append({
                    "symbol": symbol,
                    "quantity": ib_pos["quantity"],
                    "current_price": round(ib_pos["current_price"], 2),
                    "note": "Position not in trade history - may be old or from elsewhere",
                })
                reconciliation["status"] = "DISCREPANCY"
            
            elif hist_pos:
                # In history only - not in IB (possibly closed without our knowledge)
                reconciliation["history_only"].append({
                    "symbol": symbol,
                    "expected_qty": hist_pos["quantity"],
                    "note": "Expected open but not found in IB - verify if closed",
                })
                reconciliation["status"] = "MISMATCH"
        
        return reconciliation
    
    def calculate_total_unrealized_pnl(self, reconciliation: Dict[str, Any]) -> float:
        """Calculate total unrealized PnL from reconciliation."""
        total = 0.0
        
        for match in reconciliation.get("matches", []):
            total += match.get("unrealized_pnl", 0.0)
        
        return round(total, 2)
    
    def display_reconciliation(self, reconciliation: Dict[str, Any]):
        """Pretty-print reconciliation report."""
        print(f"\n{'='*70}")
        print(f"Position Reconciliation Report")
        print(f"{'='*70}\n")
        
        print(f"Status: {reconciliation['status']}\n")
        
        # Matched positions
        matches = reconciliation.get("matches", [])
        if matches:
            print(f"✓ Matched Positions ({len(matches)})\n")
            print(f"{'Symbol':<8} {'Qty':<8} {'Avg Entry':<12} {'Current':<12} {'Unrealized':<12} {'%':<8}")
            print(f"{'-'*70}")
            
            total_unrealized = 0
            for pos in matches:
                print(
                    f"{pos['symbol']:<8} "
                    f"{pos['quantity']:<8} "
                    f"${pos['entry_avg']:<11.2f} "
                    f"${pos['current_price']:<11.2f} "
                    f"${pos['unrealized_pnl']:<11.2f} "
                    f"{pos['unrealized_pnl_percent']:<7.2f}%"
                )
                total_unrealized += pos['unrealized_pnl']
            
            print(f"{'-'*70}")
            print(f"{'TOTAL':<8} {'':<8} {'':<12} {'':<12} ${total_unrealized:<11.2f}\n")
        
        # Quantity mismatches
        mismatches = reconciliation.get("quantity_mismatch", [])
        if mismatches:
            print(f"⚠ Quantity Mismatches ({len(mismatches)})\n")
            for pos in mismatches:
                print(f"  {pos['symbol']}: IB={pos['ib_qty']}, History={pos['history_qty']}, Diff={pos['discrepancy']}")
            print()
        
        # IB only
        ib_only = reconciliation.get("ib_only", [])
        if ib_only:
            print(f"? IB Only ({len(ib_only)}) - Not in trade history\n")
            for pos in ib_only:
                print(f"  {pos['symbol']}: {pos['quantity']} shares @ ${pos['current_price']}")
            print()
        
        # History only
        hist_only = reconciliation.get("history_only", [])
        if hist_only:
            print(f"? History Only ({len(hist_only)}) - Expected open but not in IB\n")
            for pos in hist_only:
                print(f"  {pos['symbol']}: {pos['expected_qty']} shares")
            print()
        
        print(f"{'='*70}\n")
    
    def export_reconciliation_json(self, reconciliation: Dict[str, Any], filename: str = "position_reconciliation.json"):
        """Save reconciliation as JSON for analysis."""
        import json
        from pathlib import Path
        
        reports_dir = Path("data/reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        filepath = reports_dir / filename
        
        with open(filepath, "w") as f:
            json.dump(reconciliation, f, indent=2, default=str)
        
        print(f"✓ Reconciliation saved to {filepath}")
