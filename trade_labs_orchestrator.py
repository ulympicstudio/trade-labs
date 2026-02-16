"""
Trade Labs Master Orchestrator

Master control module that integrates:
- Automated scanning and scoring
- Execution engine
- Position tracking and reconciliation
- Report generation
- Scheduled operations

This is the main entry point for running the complete trading system.
"""

import os
from datetime import datetime
from typing import Optional, Dict, Any

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.signals.run_full_pipeline import run_full_pipeline
from src.utils.log_manager import setup_logging, PipelineLogger
from src.utils.trade_history_db import TradeHistoryDB
from src.utils.report_generator import ReportGenerator
from src.utils.position_reconciler import PositionReconciler
from src.utils.scheduler import create_standard_schedule, PipelineScheduler
from src.data.ib_market_data import connect_ib


class TradeLabsOrchestrator:
    """
    Master orchestrator for Trade Labs.
    
    Manages:
    - Pipeline execution
    - Trade history tracking
    - Reporting and reconciliation
    - Scheduler for automated operations
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize orchestrator with optional configuration."""
        self.config = config or {}
        
        # Initialize components
        self.db = TradeHistoryDB(self.config.get("db_dir", "data/trade_history"))
        self.reporter = ReportGenerator(self.db)
        self.reconciler = PositionReconciler(self.db)
        self.scheduler = None
        
        # Setup logging
        import inspect as _insp
        import src.utils.log_manager as _lm
        print(f"[DIAG] log_manager loaded from: {_lm.__file__}")
        print(f"[DIAG] setup_logging signature: {_insp.signature(_lm.setup_logging)}")
        setup_logging(
            "trade_labs_orchestrator",
            log_dir=self.config.get("log_dir", "logs/pipeline"),
        )
        self.logger = PipelineLogger.get_logger()
        
        print(f"\n{'='*60}")
        print(f"{SYSTEM_NAME} → {HUMAN_NAME}: ORCHESTRATOR v1")
        print(f"{'='*60}\n")
        print(f"Mode: {os.getenv('TRADE_LABS_MODE', 'PAPER')}")
        print(f"Backend: {os.getenv('TRADE_LABS_EXECUTION_BACKEND', 'SIM')}")
        print(f"Armed: {os.getenv('TRADE_LABS_ARMED', '0')}")
        if os.getenv("TRADE_LABS_ARMED", "0") == "1":
            print("WARNING: TRADE_LABS_ARMED=1 (IB paper orders can be submitted).")
        print()
    
    def run_pipeline(
        self,
        num_candidates: int = 5,
        use_spy_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Run the complete trading pipeline once.
        
        Returns:
            Pipeline execution results
        """
        try:
            result = run_full_pipeline(
                num_candidates=num_candidates,
                use_spy_only=use_spy_only,
            )
            return result
        except Exception as e:
            print(f"Error running pipeline: {str(e)}")
            return {"ok": False, "error": str(e)}
    
    def reconcile_positions(self) -> Dict[str, Any]:
        """
        Reconcile open positions with IB.
        
        Returns:
            Reconciliation results
        """
        try:
            print(f"\n{'='*60}")
            print("Position Reconciliation")
            print(f"{'='*60}\n")
            
            ib = connect_ib()
            reconciliation = self.reconciler.reconcile(ib)
            ib.disconnect()
            
            self.reconciler.display_reconciliation(reconciliation)
            self.reconciler.export_reconciliation_json(reconciliation)
            
            return reconciliation
        except Exception as e:
            print(f"Error reconciling positions: {str(e)}")
            return {"error": str(e), "status": "ERROR"}
    
    def generate_daily_report(self, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate daily trading report.
        
        Returns:
            Report data
        """
        try:
            print(f"\n{'='*60}")
            print("Daily Report Generation")
            print(f"{'='*60}\n")
            
            report = self.reporter.generate_daily_report(date)
            self.reporter.display_report(report)
            
            # Save reports
            self.reporter.save_report_markdown(report)
            self.reporter.save_report_csv(report)
            
            return report
        except Exception as e:
            print(f"Error generating report: {str(e)}")
            return {"error": str(e)}
    
    def get_trading_stats(self) -> Dict[str, Any]:
        """Get overall trading statistics."""
        return self.db.get_stats()
    
    def display_stats(self):
        """Display trading statistics."""
        stats = self.get_trading_stats()
        
        print(f"\n{'='*60}")
        print("Trading Statistics")
        print(f"{'='*60}\n")
        print(f"Pipeline Runs:     {stats['pipeline_runs']}")
        print(f"Total Trades:      {stats['total_trades']}")
        print(f"Closed Trades:     {stats['closed_trades']}")
        print(f"Open Trades:       {stats['open_trades']}")
        print(f"Wins:              {stats['wins']}")
        print(f"Losses:            {stats['losses']}")
        print(f"Win Rate:          {stats['win_rate']:.2f}%")
        print(f"Total PnL:         ${stats['total_pnl']:,.2f}")
        print(f"Avg Trade PnL:     ${stats['avg_trade_pnl']:,.2f}")
        print(f"{'='*60}\n")
    
    def create_scheduler(self) -> PipelineScheduler:
        """
        Create scheduler for automated operations.
        
        Returns:
            Configured scheduler
        """
        self.scheduler = create_standard_schedule(
            pipeline_fn=self.run_pipeline,
            reconciliation_fn=self.reconcile_positions,
            report_fn=self.generate_daily_report,
        )
        return self.scheduler
    
    def start_scheduler(self):
        """Start the scheduler."""
        if self.scheduler is None:
            self.create_scheduler()
        
        self.scheduler.start()
        self._display_menu()
    
    def stop_scheduler(self):
        """Stop the scheduler."""
        if self.scheduler:
            self.scheduler.stop()
    
    def _display_menu(self):
        """Display interactive menu."""
        print("\n" + "="*60)
        print("Trade Labs Scheduler Running ✓")
        print("="*60)
        print("\nScheduler is running in background. Available commands:\n")
        print("  status    - Show scheduler status")
        print("  run       - Run pipeline now")
        print("  report    - Generate report now")
        print("  reconcile - Reconcile positions now")
        print("  stats     - Show trading stats")
        print("  quit      - Stop scheduler and exit")
        print("\nEnter command (or 'quit' to exit):\n")


def main():
    """Main entry point for Trade Labs orchestrator."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Trade Labs Master Orchestrator"
    )
    parser.add_argument(
        "--mode",
        choices=["pipeline", "reconcile", "report", "scheduler", "stats"],
        default="pipeline",
        help="Operation mode",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=5,
        help="Number of candidates to execute",
    )
    parser.add_argument(
        "--spy-only",
        action="store_true",
        help="Test mode: trade SPY only",
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for report (YYYY-MM-DD)",
    )
    
    args = parser.parse_args()
    
    # Create orchestrator
    orchestrator = TradeLabsOrchestrator()
    
    if args.mode == "pipeline":
        orchestrator.run_pipeline(
            num_candidates=args.candidates,
            use_spy_only=args.spy_only,
        )
    
    elif args.mode == "reconcile":
        orchestrator.reconcile_positions()
    
    elif args.mode == "report":
        orchestrator.generate_daily_report(args.date)
    
    elif args.mode == "stats":
        orchestrator.display_stats()
    
    elif args.mode == "scheduler":
        orchestrator.start_scheduler()


if __name__ == "__main__":
    main()
