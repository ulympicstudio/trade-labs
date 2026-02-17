"""
Market hours utility: check if US equity market is open now.
"""
from datetime import datetime
import pytz

# US Eastern timezone (market timezone)
ET = pytz.timezone("US/Eastern")


def is_market_open() -> bool:
    """
    Check if US equity market is open right now.
    - Monday-Friday only
    - 9:30 AM - 4:00 PM ET
    Returns True if market is open, False otherwise.
    """
    now = datetime.now(ET)
    
    # Market is closed on weekends (5=Sat, 6=Sun)
    if now.weekday() >= 5:
        return False
    
    # Market hours: 9:30 AM to 4:00 PM ET
    market_open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    
    return market_open_time <= now < market_close_time
