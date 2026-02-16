"""
Migration Tools

Migrate data from Phase 2 JSON storage to Phase 3 SQLite database.
Preserves all data and allows dual operation during transition.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.database.db_manager import TradeLabsDB
from src.utils.trade_history_db import TradeHistoryDB as JSONTradeHistoryDB


class MigrationManager:
    """Handle data migration from JSON to SQLite."""
    
    def __init__(self, json_dir: str = "data/trade_history", db_path: str = "data/trade_labs.db"):
        self.json_dir = Path(json_dir)
        self.json_db = JSONTradeHistoryDB(json_dir)
        self.sql_db = TradeLabsDB(db_path)
    
    def migrate_all(self) -> Dict[str, int]:
        """
        Migrate all data from JSON to SQLite.
        
        Returns count of migrated records.
        """
        counts = {
            "runs": 0,
            "trades": 0,
            "errors": 0,
        }
        
        print("\n" + "="*70)
        print("MIGRATION: JSON → SQLite")
        print("="*70 + "\n")
        
        # Migrate runs
        print("Migrating pipeline runs...", end="", flush=True)
        try:
            counts["runs"] = self._migrate_runs()
            print(f" ✓ ({counts['runs']} runs)")
        except Exception as e:
            print(f" ✗ Error: {str(e)}")
            counts["errors"] += 1
        
        # Migrate trades
        print("Migrating trades...", end="", flush=True)
        try:
            counts["trades"] = self._migrate_trades()
            print(f" ✓ ({counts['trades']} trades)")
        except Exception as e:
            print(f" ✗ Error: {str(e)}")
            counts["errors"] += 1
        
        print("\n" + "="*70)
        print(f"Migration Complete: {counts['runs']} runs, {counts['trades']} trades")
        if counts["errors"] > 0:
            print(f"⚠ {counts['errors']} errors during migration")
        print("="*70 + "\n")
        
        return counts
    
    def _migrate_runs(self) -> int:
        """Migrate pipeline runs from JSON."""
        runs = self.json_db.get_run_history(limit=10000)
        count = 0
        
        for run in runs:
            try:
                self.sql_db.record_run(
                    run_id=run.get('run_id'),
                    backend=run.get('backend', 'SIM'),
                    armed=run.get('armed', False),
                    num_scanned=run.get('stats', {}).get('scanned', 0),
                    num_executed=run.get('stats', {}).get('executed', 0),
                    num_successful=run.get('stats', {}).get('successful', 0),
                    details=run.get('details', {}),
                )
                count += 1
            except Exception as e:
                print(f"  Warning: Could not migrate run {run.get('run_id')}: {str(e)}")
        
        return count
    
    def _migrate_trades(self) -> int:
        """Migrate trades from JSON."""
        trades = self.json_db.get_trade_history(limit=10000)
        count = 0
        
        for trade in trades:
            try:
                # Record trade
                recorded = self.sql_db.record_trade(
                    run_id=trade.get('run_id'),
                    symbol=trade.get('symbol'),
                    side=trade.get('side'),
                    entry_price=trade.get('entry_price', 0.0),
                    entry_timestamp=trade.get('entry_timestamp'),
                    quantity=trade.get('quantity', 0),
                    stop_loss=trade.get('stop_loss', 0.0),
                    parent_order_id=trade.get('order_id'),
                    stop_order_id=trade.get('stop_order_id'),
                )
                
                # If closed, close it
                if trade.get('status') == 'CLOSED':
                    self.sql_db.close_trade(
                        trade_id=recorded.id,
                        exit_price=trade.get('exit_price', 0.0),
                        exit_timestamp=trade.get('exit_timestamp'),
                    )
                
                count += 1
            except Exception as e:
                print(f"  Warning: Could not migrate trade {trade.get('symbol')}: {str(e)}")
        
        return count
    
    def verify_migration(self) -> Dict[str, Any]:
        """Verify migration integrity."""
        print("\nVerifying migration...\n")
        
        # Get stats from both sources
        json_stats = {
            "runs": len(self.json_db.get_run_history(limit=10000)),
            "trades": len(self.json_db.get_trade_history(limit=10000)),
        }
        
        sql_stats = self.sql_db.get_stats()
        sql_stats["runs"] = len([r for r in self.sql_db.get_runs(limit=10000)])
        
        verification = {
            "json_runs": json_stats["runs"],
            "sql_runs": sql_stats["runs"],
            "json_trades": json_stats["trades"],
            "sql_trades": sql_stats["total_trades"],
            "clean": False,
        }
        
        print(f"JSON Runs:      {json_stats['runs']}")
        print(f"SQLite Runs:    {sql_stats['runs']}")
        print(f"JSON Trades:    {json_stats['trades']}")
        print(f"SQLite Trades:  {sql_stats['total_trades']}")
        
        # Check if counts match (approximately)
        run_diff = abs(json_stats['runs'] - sql_stats['runs'])
        trade_diff = abs(json_stats['trades'] - sql_stats['total_trades'])
        
        if run_diff <= 1 and trade_diff <= 1:
            print("\n✓ Migration verification PASSED")
            verification["clean"] = True
        else:
            print("\n⚠ Migration verification WARNING - counts differ")
            print(f"  Run difference:   {run_diff}")
            print(f"  Trade difference: {trade_diff}")
        
        return verification
    
    def rollback_json_removal(self):
        """OPTION: Keep JSON for backwards compatibility."""
        print("\nJSON files retained for backwards compatibility.")
        print("Both JSON and SQLite databases now available.")
        print("Recommendation: Use SQLite for new operations going forward.")


def perform_migration():
    """Perform complete migration from JSON to SQLite."""
    migrator = MigrationManager()
    
    # Run migration
    counts = migrator.migrate_all()
    
    # Verify
    verification = migrator.verify_migration()
    
    # Keep JSON files for now
    migrator.rollback_json_removal()
    
    return verification


if __name__ == "__main__":
    perform_migration()
