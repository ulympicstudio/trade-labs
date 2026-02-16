"""
Pipeline Scheduler

Automatically runs the trading pipeline at scheduled times:
- Market open (9:30 AM ET)
- Pre-market scan (before market open)
- Post-market reconciliation (after market close)
- Daily report generation

Uses APScheduler for robust scheduling with timezone support.
"""

import pytz
from datetime import datetime, time
from typing import Optional, Callable, Any
from abc import ABC, abstractmethod

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    print("WARNING: APScheduler not installed. Scheduler will not work.")
    print("Install with: pip install apscheduler")


class PipelineScheduler:
    """
    Schedule pipeline runs at specific times.
    
    Times are in US/Eastern (market timezone).
    """
    
    def __init__(self):
        if not HAS_APSCHEDULER:
            raise RuntimeError("APScheduler is required. Install with: pip install apscheduler")
        
        self.scheduler = BackgroundScheduler()
        self.tz = pytz.timezone('US/Eastern')
        self.jobs = {}
    
    def schedule_market_open_scan(
        self,
        pipeline_fn: Callable[..., Any],
        num_candidates: int = 5,
        name: str = "market_open_scan",
    ):
        """
        Schedule pipeline to run at market open (9:30 AM ET).
        
        Runs Monday-Friday during market hours.
        """
        job = self.scheduler.add_job(
            pipeline_fn,
            trigger=CronTrigger(hour=9, minute=30, day_of_week='mon-fri', tz=self.tz),
            kwargs={"num_candidates": num_candidates},
            id=name,
            name=f"Pipeline: {name}",
            replace_existing=True,
        )
        
        self.jobs[name] = job
        print(f"✓ Scheduled: Market open scan (9:30 AM ET, Mon-Fri)")
        return job
    
    def schedule_mid_day_scan(
        self,
        pipeline_fn: Callable[..., Any],
        hour: int = 12,
        minute: int = 0,
        num_candidates: int = 3,
        name: str = "mid_day_scan",
    ):
        """
        Schedule pipeline to run at midday.
        
        Default: 12:00 PM ET
        """
        job = self.scheduler.add_job(
            pipeline_fn,
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week='mon-fri', tz=self.tz),
            kwargs={"num_candidates": num_candidates},
            id=name,
            name=f"Pipeline: {name}",
            replace_existing=True,
        )
        
        self.jobs[name] = job
        print(f"✓ Scheduled: Mid-day scan ({hour}:{minute:02d} AM ET, Mon-Fri)")
        return job
    
    def schedule_market_close_reconciliation(
        self,
        reconciliation_fn: Callable[..., Any],
        name: str = "market_close_reconciliation",
    ):
        """
        Schedule position reconciliation after market close (4:00 PM ET).
        """
        job = self.scheduler.add_job(
            reconciliation_fn,
            trigger=CronTrigger(hour=16, minute=0, day_of_week='mon-fri', tz=self.tz),
            id=name,
            name=f"Reconciliation: {name}",
            replace_existing=True,
        )
        
        self.jobs[name] = job
        print(f"✓ Scheduled: Market close reconciliation (4:00 PM ET, Mon-Fri)")
        return job
    
    def schedule_daily_report(
        self,
        report_fn: Callable[..., Any],
        hour: int = 17,
        minute: int = 0,
        name: str = "daily_report",
    ):
        """
        Schedule daily report generation.
        
        Default: 5:00 PM ET (after market close)
        """
        job = self.scheduler.add_job(
            report_fn,
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week='mon-fri', tz=self.tz),
            id=name,
            name=f"Report: {name}",
            replace_existing=True,
        )
        
        self.jobs[name] = job
        print(f"✓ Scheduled: Daily report ({hour}:{minute:02d} PM ET, Mon-Fri)")
        return job
    
    def schedule_custom(
        self,
        fn: Callable[..., Any],
        hour: int,
        minute: int,
        name: str,
        kwargs: Optional[dict] = None,
    ):
        """
        Schedule a custom function at a specific time.
        
        Time is in US/Eastern.
        """
        job = self.scheduler.add_job(
            fn,
            trigger=CronTrigger(hour=hour, minute=minute, day_of_week='mon-fri', tz=self.tz),
            kwargs=kwargs or {},
            id=name,
            name=name,
            replace_existing=True,
        )
        
        self.jobs[name] = job
        print(f"✓ Scheduled: {name} at {hour}:{minute:02d} ET")
        return job
    
    def start(self):
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            print(f"\n✓ Scheduler started (timezone: {self.tz})")
            self._display_schedule()
    
    def stop(self):
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            print("✓ Scheduler stopped")
    
    def pause(self):
        """Pause the scheduler without stopping it."""
        self.scheduler.pause()
        print("✓ Scheduler paused")
    
    def resume(self):
        """Resume the paused scheduler."""
        self.scheduler.resume()
        print("✓ Scheduler resumed")
    
    def _display_schedule(self):
        """Display the current schedule."""
        print(f"\n{'='*60}")
        print("Scheduled Jobs")
        print(f"{'='*60}\n")
        
        for job in self.scheduler.get_jobs():
            trigger = job.trigger
            next_run = job.next_run_time
            
            print(f"Name: {job.name}")
            print(f"Trigger: {trigger}")
            if next_run:
                print(f"Next Run: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print()
    
    def get_status(self) -> dict:
        """Get scheduler status."""
        jobs = []
        
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "trigger": str(job.trigger),
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        
        return {
            "running": self.scheduler.running,
            "paused": self.scheduler._paused if hasattr(self.scheduler, '_paused') else False,
            "jobs_count": len(jobs),
            "jobs": jobs,
        }


def create_standard_schedule(
    pipeline_fn: Callable[..., Any],
    reconciliation_fn: Optional[Callable[..., Any]] = None,
    report_fn: Optional[Callable[..., Any]] = None,
) -> PipelineScheduler:
    """
    Create a standard trading schedule.
    
    Runs:
    - 9:30 AM: Market open scan (5 candidates)
    - 12:00 PM: Mid-day scan (3 candidates)
    - 4:00 PM: Position reconciliation (if provided)
    - 5:00 PM: Daily report (if provided)
    """
    
    scheduler = PipelineScheduler()
    
    # Market open scan
    scheduler.schedule_market_open_scan(pipeline_fn, num_candidates=5)
    
    # Mid-day scan
    scheduler.schedule_mid_day_scan(pipeline_fn, hour=12, minute=0, num_candidates=3)
    
    # Post-market reconciliation
    if reconciliation_fn:
        scheduler.schedule_market_close_reconciliation(reconciliation_fn)
    
    # Daily report
    if report_fn:
        scheduler.schedule_daily_report(report_fn, hour=17, minute=0)
    
    return scheduler
