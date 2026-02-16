"""
Daily Report Generator

Parses trade history and generates performance reports:
- Daily PnL summary
- Weekly PnL summary
- Monthly PnL summary
- Win rate and trade metrics
- Risk analysis

Generated reports are saved to CSV and markdown for easy reviewing.
"""

import json
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict

from src.utils.trade_history_db import TradeHistoryDB


class ReportGenerator:
    """Generate trading performance reports from trade history."""
    
    def __init__(self, db: Optional[TradeHistoryDB] = None, reports_dir: str = "data/reports"):
        self.db = db or TradeHistoryDB("data/trade_history")
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
    
    def generate_daily_report(self, date: Optional[str] = None) -> Dict[str, Any]:
        """Generate a report for a specific day."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        
        summary = self.db.get_daily_summary(date)
        trades = [t for t in self.db.get_trade_history(status="CLOSED") 
                  if t["entry_timestamp"].startswith(date)]
        
        report = {
            "date": date,
            "generated_at": datetime.utcnow().isoformat(),
            "summary": summary,
            "trades": trades,
            "metrics": self._calculate_metrics(trades),
        }
        
        return report
    
    def generate_weekly_report(self, week_start: Optional[str] = None) -> Dict[str, Any]:
        """Generate a report for a specific week."""
        if week_start is None:
            today = datetime.utcnow()
            week_start_date = today - timedelta(days=today.weekday())
            week_start = week_start_date.strftime("%Y-%m-%d")
        
        week_end_date = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)
        week_end = week_end_date.strftime("%Y-%m-%d")
        
        trades = [t for t in self.db.get_trade_history(status="CLOSED")
                  if week_start <= t["entry_timestamp"][:10] < week_end]
        
        total_pnl = sum(t.get("pnl", 0.0) for t in trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
        
        report = {
            "period": f"{week_start} to {week_end}",
            "generated_at": datetime.utcnow().isoformat(),
            "summary": {
                "trades": len(trades),
                "total_pnl": round(total_pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / len(trades) * 100) if trades else 0, 2),
            },
            "trades": trades,
            "metrics": self._calculate_metrics(trades),
        }
        
        return report
    
    def generate_monthly_report(self, month: Optional[str] = None) -> Dict[str, Any]:
        """Generate a report for a specific month (YYYY-MM)."""
        if month is None:
            month = datetime.utcnow().strftime("%Y-%m")
        
        trades = [t for t in self.db.get_trade_history(status="CLOSED")
                  if t["entry_timestamp"].startswith(month)]
        
        total_pnl = sum(t.get("pnl", 0.0) for t in trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
        
        report = {
            "period": month,
            "generated_at": datetime.utcnow().isoformat(),
            "summary": {
                "trades": len(trades),
                "total_pnl": round(total_pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / len(trades) * 100) if trades else 0, 2),
            },
            "trades": trades,
            "metrics": self._calculate_metrics(trades),
        }
        
        return report
    
    def _calculate_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate advanced trading metrics."""
        if not trades:
            return {
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "largest_win": 0.0,
                "largest_loss": 0.0,
                "profit_factor": 0.0,
                "avg_trade_duration": 0,
            }
        
        winning_trades = [t for t in trades if t.get("pnl", 0) > 0]
        losing_trades = [t for t in trades if t.get("pnl", 0) < 0]
        
        avg_win = sum(t.get("pnl", 0) for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(t.get("pnl", 0) for t in losing_trades) / len(losing_trades) if losing_trades else 0
        
        largest_win = max((t.get("pnl", 0) for t in winning_trades), default=0)
        largest_loss = min((t.get("pnl", 0) for t in losing_trades), default=0)
        
        gross_profit = sum(t.get("pnl", 0) for t in winning_trades)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losing_trades))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Calculate average trade duration
        durations = []
        for t in trades:
            if t.get("exit_timestamp") and t.get("entry_timestamp"):
                entry = datetime.fromisoformat(t["entry_timestamp"])
                exit_time = datetime.fromisoformat(t["exit_timestamp"])
                duration = (exit_time - entry).total_seconds() / 3600  # hours
                durations.append(duration)
        
        avg_duration = sum(durations) / len(durations) if durations else 0
        
        return {
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "largest_win": round(largest_win, 2),
            "largest_loss": round(largest_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_trade_duration_hours": round(avg_duration, 2),
        }
    
    def save_report_csv(self, report: Dict[str, Any], filename: Optional[str] = None):
        """Save trades from report as CSV."""
        if filename is None:
            period = report.get("period", report.get("date", "all"))
            filename = f"report_{period}.csv"
        
        csv_path = self.reports_dir / filename
        
        trades = report.get("trades", [])
        if not trades:
            return
        
        fieldnames = [
            "symbol", "side", "entry_price", "quantity", "stop_loss",
            "exit_price", "pnl", "pnl_percent", "entry_timestamp",
            "exit_timestamp", "status"
        ]
        
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for trade in trades:
                writer.writerow({k: trade.get(k, "") for k in fieldnames})
    
    def save_report_markdown(self, report: Dict[str, Any], filename: Optional[str] = None):
        """Save report summary as Markdown."""
        if filename is None:
            period = report.get("period", report.get("date", "all"))
            filename = f"report_{period}.md"
        
        md_path = self.reports_dir / filename
        
        with open(md_path, "w") as f:
            # Header
            period = report.get("period", report.get("date", "Unknown"))
            f.write(f"# Trading Report: {period}\n\n")
            f.write(f"*Generated: {report.get('generated_at', 'Unknown')}*\n\n")
            
            # Summary
            f.write("## Summary\n\n")
            summary = report.get("summary", {})
            f.write(f"| Metric | Value |\n")
            f.write(f"|--------|-------|\n")
            f.write(f"| Trades | {summary.get('trades', 0)} |\n")
            f.write(f"| Total PnL | ${summary.get('total_pnl', 0):,.2f} |\n")
            f.write(f"| Wins | {summary.get('wins', 0)} |\n")
            f.write(f"| Losses | {summary.get('losses', 0)} |\n")
            f.write(f"| Win Rate | {summary.get('win_rate', 0):.2f}% |\n\n")
            
            # Metrics
            f.write("## Metrics\n\n")
            metrics = report.get("metrics", {})
            f.write(f"| Metric | Value |\n")
            f.write(f"|--------|-------|\n")
            f.write(f"| Avg Win | ${metrics.get('avg_win', 0):,.2f} |\n")
            f.write(f"| Avg Loss | ${metrics.get('avg_loss', 0):,.2f} |\n")
            f.write(f"| Largest Win | ${metrics.get('largest_win', 0):,.2f} |\n")
            f.write(f"| Largest Loss | ${metrics.get('largest_loss', 0):,.2f} |\n")
            f.write(f"| Profit Factor | {metrics.get('profit_factor', 0)} |\n")
            f.write(f"| Avg Trade Duration | {metrics.get('avg_trade_duration_hours', 0)} hours |\n\n")
            
            # Trades
            if report.get("trades"):
                f.write("## Trades\n\n")
                f.write(f"| Symbol | Side | Entry | Exit | Shares | PnL | PnL% |\n")
                f.write(f"|--------|------|-------|------|--------|-----|------|\n")
                
                for trade in report.get("trades", []):
                    f.write(
                        f"| {trade.get('symbol', 'N/A')} | "
                        f"{trade.get('side', 'N/A')} | "
                        f"${trade.get('entry_price', 0):.2f} | "
                        f"${trade.get('exit_price', 0):.2f} | "
                        f"{trade.get('quantity', 0)} | "
                        f"${trade.get('pnl', 0):,.2f} | "
                        f"{trade.get('pnl_percent', 0):.2f}% |\n"
                    )
    
    def generate_all_reports_for_date(self, date: Optional[str] = None):
        """Generate all report types (markdown + CSV) for a date."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        
        report = self.generate_daily_report(date)
        
        # Save both formats
        self.save_report_markdown(report, f"daily_{date}.md")
        self.save_report_csv(report, f"daily_{date}.csv")
        
        print(f"✓ Generated daily reports for {date}")
        return report
    
    def display_report(self, report: Dict[str, Any]):
        """Pretty-print a report to console."""
        period = report.get("period", report.get("date", "Unknown"))
        print(f"\n{'='*60}")
        print(f"Trading Report: {period}")
        print(f"{'='*60}\n")
        
        summary = report.get("summary", {})
        print(f"Trades:   {summary.get('trades', 0)}")
        print(f"PnL:      ${summary.get('total_pnl', 0):,.2f}")
        print(f"Wins:     {summary.get('wins', 0)}")
        print(f"Losses:   {summary.get('losses', 0)}")
        print(f"Win Rate: {summary.get('win_rate', 0):.2f}%\n")
        
        metrics = report.get("metrics", {})
        print(f"Avg Win:     ${metrics.get('avg_win', 0):,.2f}")
        print(f"Avg Loss:    ${metrics.get('avg_loss', 0):,.2f}")
        print(f"Largest Win: ${metrics.get('largest_win', 0):,.2f}")
        print(f"Largest Loss: ${metrics.get('largest_loss', 0):,.2f}")
        print(f"Profit Factor: {metrics.get('profit_factor', 0)}")
        print(f"{'='*60}\n")
