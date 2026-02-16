"""
Full Automated Pipeline Orchestrator

Connects scanner → scoring → sizing → execution
into a single run-and-forget workflow.

Flow:
1. Connect to IB
2. Get account equity + open risk
3. Scan for candidates
4. Score and select top N
5. Execute each via pipeline
6. Report results
"""

import os
import uuid
from typing import List, Dict, Any
from datetime import datetime

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.data.ib_market_data import (
    connect_ib,
    get_spy_contract,
    get_history_bars,
    get_recent_price_from_history,
    get_account_equity_usd,
)
from src.indicators.atr import compute_atr
from src.risk.open_risk import estimate_open_risk_usd
from src.signals.signal_engine import get_trade_intents_from_scan
from src.execution.pipeline import execute_trade_intent_paper
from src.utils.log_manager import setup_logging, PipelineLogger
from src.utils.trade_history_db import TradeHistoryDB


def run_full_pipeline(
    num_candidates: int = 5,
    use_spy_only: bool = False,
) -> Dict[str, Any]:
    """
    Run complete pipeline: scan → score → execute.
    
    Args:
        num_candidates: How many top-scored candidates to execute
        use_spy_only: For testing, only trade SPY (ignores scanner)
    
    Returns:
        Dict with pipeline results
    """
    
    # Initialize run tracking
    run_id = str(uuid.uuid4())[:8]
    setup_logging("trade_labs", log_dir="logs/pipeline")
    logger = PipelineLogger.get_logger("pipeline_orchestrator")
    logger.scan_started(run_id)
    
    db = TradeHistoryDB("data/trade_history")
    
    os.environ["TRADE_LABS_MODE"] = "PAPER"
    
    # Default to SIM unless explicitly armed
    if os.getenv("TRADE_LABS_EXECUTION_BACKEND") is None:
        os.environ["TRADE_LABS_EXECUTION_BACKEND"] = "SIM"
    
    backend = os.getenv('TRADE_LABS_EXECUTION_BACKEND', 'SIM')
    armed = os.getenv('TRADE_LABS_ARMED', '0') == '1'
    
    print(f"\n{'='*60}")
    print(f"{SYSTEM_NAME} → {HUMAN_NAME}: FULL PIPELINE v1")
    print(f"{'='*60}\n")
    print(f"Run ID: {run_id}")
    print(f"Mode: PAPER  |  Backend: {backend}  |  Armed: {armed}\n")
    
    ib = connect_ib()
    
    # Get account metrics
    account_equity_usd = get_account_equity_usd(ib)
    print(f"Account Equity: ${account_equity_usd:,.2f}\n")
    
    # Get reference ATR from SPY for common stop calculation
    spy_atr = 8.0  # Fallback default
    try:
        spy_contract = get_spy_contract()
        spy_bars = get_history_bars(ib, spy_contract, duration="30 D", bar_size="1 day")
        spy_atr = compute_atr(spy_bars, period=14)
    except Exception as e:
        print(f"(Warning: Could not fetch SPY ATR, using default {spy_atr:.2f})\n")
    
    print(f"SPY ATR(14): {spy_atr:.4f}\n")
    
    # Get current open risk
    open_risk_usd = estimate_open_risk_usd(ib, atr=spy_atr, atr_multiplier=2.0)
    print(f"Current Open Risk: ${open_risk_usd:,.2f}\n")
    
    # Generate trade intents from scanner
    if use_spy_only:
        print("(Using SPY-only mode for testing)\n")
        from src.contracts.trade_intent import TradeIntent
        intents = [
            TradeIntent(
                symbol="SPY",
                side="BUY",
                entry_type="MKT",
                quantity=None,
                stop_loss=None,
                rationale="Test: SPY"
            )
        ]
    else:
        print(f"Scanning for top {num_candidates} candidates...\n")
        intents = get_trade_intents_from_scan(ib, limit=50)
        intents = intents[:num_candidates]
        
        if not intents:
            print("No tradeable candidates found.\n")
            ib.disconnect()
            return {"ok": False, "reason": "No candidates"}
        
        print(f"Selected {len(intents)} candidates:\n")
        for i, intent in enumerate(intents, 1):
            print(f"  {i}. {intent.symbol} — {intent.rationale}")
        print()
    
    # Execute each candidate
    results = []
    executed_count = 0
    successful_count = 0
    
    for intent in intents:
        print(f"\n{'─'*60}")
        print(f"Executing: {intent.symbol}")
        print(f"{'─'*60}")
        
        try:
            # Convert symbol to contract first
            from src.signals.market_scanner import to_contract
            contract = to_contract(intent.symbol)
            
            # Get current price
            entry_price = get_recent_price_from_history(ib, contract)
            print(f"Entry Price: ${entry_price:.2f}")
            
            # Get ATR for this symbol with fallback
            atr = spy_atr  # Use default if retrieval fails
            try:
                bars = get_history_bars(ib, contract, duration="30 D", bar_size="1 day")
                atr = compute_atr(bars, period=14)
                print(f"ATR(14): {atr:.4f}")
            except Exception as atr_err:
                print(f"(Using default ATR {atr:.4f})")
            
            # Execute via pipeline
            result = execute_trade_intent_paper(
                intent=intent,
                ib=ib,
                account_equity_usd=account_equity_usd,
                entry_price=entry_price,
                open_risk_usd=open_risk_usd,
                atr=atr,
                atr_multiplier=2.0,
                risk_percent=0.005  # 0.5%
            )
            
            executed_count += 1
            
            results.append({
                "symbol": intent.symbol,
                "ok": result.get("ok", False),
                "result": result
            })
            
            if result.get("ok"):
                successful_count += 1
                print(f"✓ Success: {result.get('sized_shares', 'N/A')} shares @ ${entry_price:.2f}")
                
                # Record to trade history database
                try:
                    order_result = result.get('order_result')
                    if order_result:
                        db.record_trade(
                            run_id=run_id,
                            symbol=intent.symbol,
                            side=intent.side,
                            entry_price=entry_price,
                            quantity=result.get('sized_shares', 0),
                            stop_loss=result.get('stop_price', 0.0),
                            order_result=order_result,
                            timestamp=datetime.utcnow().isoformat(),
                        )
                        
                        logger.execution_completed(
                            run_id=run_id,
                            symbol=intent.symbol,
                            shares=result.get('sized_shares', 0),
                            entry_price=entry_price,
                            stop_loss=result.get('stop_price', 0.0),
                            order_id=order_result.parent_order_id,
                            ok=True,
                        )
                except Exception as record_err:
                    print(f"  (Warning: Could not record trade to history: {str(record_err)})")
            else:
                print(f"✗ Blocked: {result.get('reason', 'Unknown')}")
                logger.execution_completed(
                    run_id=run_id,
                    symbol=intent.symbol,
                    shares=0,
                    entry_price=entry_price,
                    stop_loss=0.0,
                    order_id=None,
                    ok=False,
                    reason=result.get('reason', 'Unknown'),
                )
        
        except Exception as e:
            executed_count += 1
            print(f"✗ Error: {str(e)[:100]}")
            results.append({
                "symbol": intent.symbol,
                "ok": False,
                "error": str(e)
            })
            logger.execution_completed(
                run_id=run_id,
                symbol=intent.symbol,
                shares=0,
                entry_price=0.0,
                stop_loss=0.0,
                order_id=None,
                ok=False,
                reason=f"Error: {str(e)[:50]}",
            )
    
    ib.disconnect()
    
    # Record pipeline run to history
    try:
        db.record_pipeline_run(
            run_id=run_id,
            backend=backend,
            armed=armed,
            num_candidates_scanned=len(intents),
            num_candidates_executed=executed_count,
            num_successful=successful_count,
            details={
                "account_equity": account_equity_usd,
                "open_risk": open_risk_usd,
                "spy_atr": spy_atr,
                "use_spy_only": use_spy_only,
            }
        )
        logger.pipeline_completed(run_id, executed_count, successful_count)
    except Exception as e:
        print(f"Warning: Could not save pipeline run to history: {str(e)}")
    
    # Summary
    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"Executed: {executed_count} candidates  |  Successful: {successful_count}")
    print(f"{'='*60}\n")
    
    return {
        "ok": True,
        "run_id": run_id,
        "account_equity_usd": account_equity_usd,
        "open_risk_usd": open_risk_usd,
        "spy_atr": spy_atr,
        "candidates_executed": executed_count,
        "successful_executions": successful_count,
        "results": results
    }


if __name__ == "__main__":
    # Test with SPY only first
    run_full_pipeline(num_candidates=5, use_spy_only=False)
