"""
Earnings Calendar & Historical Analysis
Tracks upcoming earnings and analyzes historical price movements post-earnings.
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional, Dict
import requests


logger = logging.getLogger(__name__)


@dataclass
class EarningsEvent:
    """Upcoming earnings event."""
    symbol: str
    company_name: str
    report_date: str
    fiscal_quarter: str
    fiscal_year: int
    
    # Estimates (if available)
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None
    
    # Timing
    when: str = "bmo"  # "bmo" (before market open) or "amc" (after market close)
    days_until: int = 0
    
    # Historical performance
    historical_beat_rate: Optional[float] = None  # % of times beat estimates
    avg_move_on_beat: Optional[float] = None  # Avg % move when beats
    avg_move_on_miss: Optional[float] = None  # Avg % move when misses


@dataclass
class HistoricalEarnings:
    """Historical earnings result."""
    symbol: str
    report_date: str
    fiscal_quarter: str
    
    # Results
    eps_actual: float
    eps_estimate: float
    eps_surprise: float  # actual - estimate
    eps_surprise_pct: float  # (actual - estimate) / estimate
    
    revenue_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_surprise_pct: Optional[float] = None
    
    # Price reaction
    price_before: Optional[float] = None
    price_after_1d: Optional[float] = None
    price_after_5d: Optional[float] = None
    move_1d_pct: Optional[float] = None
    move_5d_pct: Optional[float] = None


class EarningsCalendar:
    """
    Tracks earnings calendar and analyzes historical earnings performance.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or "demo"
        self.base_url = "https://finnhub.io/api/v1"
    
    def get_upcoming_earnings(self, days_ahead: int = 30) -> List[EarningsEvent]:
        """
        Get upcoming earnings in next N days.
        
        Args:
            days_ahead: Number of days to look ahead
        
        Returns:
            List of EarningsEvent objects
        """
        events = []
        
        try:
            from_date = datetime.now().strftime('%Y-%m-%d')
            to_date = (datetime.now() + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
            
            url = f"{self.base_url}/calendar/earnings"
            params = {
                'from': from_date,
                'to': to_date,
                'token': self.api_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data.get('earningsCalendar', []):
                    report_date = datetime.strptime(item['date'], '%Y-%m-%d')
                    days_until = (report_date - datetime.now()).days
                    
                    event = EarningsEvent(
                        symbol=item['symbol'],
                        company_name=item.get('name', item['symbol']),
                        report_date=item['date'],
                        fiscal_quarter=item.get('quarter', ''),
                        fiscal_year=item.get('year', 0),
                        eps_estimate=item.get('epsEstimate'),
                        revenue_estimate=item.get('revenueEstimate'),
                        when=item.get('hour', 'bmo'),
                        days_until=days_until
                    )
                    
                    events.append(event)
                
                logger.info(f"Found {len(events)} upcoming earnings events")
            else:
                logger.warning(f"Finnhub earnings calendar returned {response.status_code}")
        
        except Exception as e:
            logger.error(f"Failed to fetch earnings calendar: {e}")
        
        return events
    
    def get_earnings_this_week(self) -> List[EarningsEvent]:
        """Get earnings events happening this week."""
        return self.get_upcoming_earnings(days_ahead=7)
    
    def get_earnings_today(self) -> List[EarningsEvent]:
        """Get earnings events happening today."""
        all_events = self.get_upcoming_earnings(days_ahead=1)
        today = datetime.now().strftime('%Y-%m-%d')
        
        return [e for e in all_events if e.report_date == today]
    
    def get_historical_earnings(self, symbol: str, limit: int = 8) -> List[HistoricalEarnings]:
        """
        Get historical earnings results for a symbol.
        
        Args:
            symbol: Stock symbol
            limit: Number of past quarters to retrieve
        
        Returns:
            List of HistoricalEarnings
        """
        results = []
        
        try:
            url = f"{self.base_url}/stock/earnings"
            params = {
                'symbol': symbol,
                'limit': limit,
                'token': self.api_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data:
                    eps_actual = item.get('actual')
                    eps_estimate = item.get('estimate')
                    
                    if eps_actual is not None and eps_estimate is not None:
                        eps_surprise = eps_actual - eps_estimate
                        eps_surprise_pct = (eps_surprise / abs(eps_estimate)) * 100 if eps_estimate != 0 else 0
                        
                        result = HistoricalEarnings(
                            symbol=item.get('symbol', symbol),
                            report_date=item.get('period', ''),
                            fiscal_quarter=item.get('quarter', ''),
                            eps_actual=eps_actual,
                            eps_estimate=eps_estimate,
                            eps_surprise=eps_surprise,
                            eps_surprise_pct=eps_surprise_pct
                        )
                        
                        results.append(result)
                
                logger.info(f"{symbol}: Retrieved {len(results)} historical earnings")
            
        except Exception as e:
            logger.error(f"Failed to fetch historical earnings for {symbol}: {e}")
        
        return results
    
    def calculate_earnings_statistics(self, symbol: str) -> dict:
        """
        Calculate historical earnings performance statistics.
        
        Returns dict with:
        - beat_rate: % of quarters that beat estimates
        - avg_surprise_pct: Average surprise percentage
        - positive_surprise_count: Number of positive surprises
        - total_quarters: Total quarters analyzed
        """
        historical = self.get_historical_earnings(symbol, limit=12)
        
        if not historical:
            return {
                'beat_rate': 0.0,
                'avg_surprise_pct': 0.0,
                'positive_surprise_count': 0,
                'total_quarters': 0
            }
        
        beats = [h for h in historical if h.eps_surprise > 0]
        surprises = [h.eps_surprise_pct for h in historical]
        
        return {
            'beat_rate': (len(beats) / len(historical)) * 100,
            'avg_surprise_pct': sum(surprises) / len(surprises),
            'positive_surprise_count': len(beats),
            'total_quarters': len(historical),
            'last_4_beat_rate': (len([h for h in historical[:4] if h.eps_surprise > 0]) / min(4, len(historical))) * 100
        }
    
    def identify_consistent_beaters(self, events: List[EarningsEvent], min_beat_rate: float = 70.0) -> List[Dict]:
        """
        Identify stocks with consistent history of beating earnings.
        
        Args:
            events: List of upcoming earnings events
            min_beat_rate: Minimum beat rate threshold (%)
        
        Returns:
            List of dicts with symbol, event, and statistics
        """
        consistent_beaters = []
        
        for event in events:
            stats = self.calculate_earnings_statistics(event.symbol)
            
            if stats['total_quarters'] >= 4 and stats['beat_rate'] >= min_beat_rate:
                consistent_beaters.append({
                    'symbol': event.symbol,
                    'event': event,
                    'stats': stats,
                    'beat_rate': stats['beat_rate'],
                    'avg_surprise': stats['avg_surprise_pct']
                })
        
        # Sort by beat rate
        consistent_beaters.sort(key=lambda x: x['beat_rate'], reverse=True)
        
        logger.info(f"Found {len(consistent_beaters)} consistent earnings beaters")
        
        return consistent_beaters
    
    def get_high_probability_earnings_plays(self, days_ahead: int = 14, min_beat_rate: float = 70.0) -> List[Dict]:
        """
        Get high-probability earnings plays based on historical performance.
        
        Args:
            days_ahead: Look ahead this many days
            min_beat_rate: Minimum historical beat rate (%)
        
        Returns:
            List of high-probability opportunities
        """
        upcoming = self.get_upcoming_earnings(days_ahead)
        beaters = self.identify_consistent_beaters(upcoming, min_beat_rate)
        
        # Enhance with timing info
        for item in beaters:
            event = item['event']
            item['days_until_earnings'] = event.days_until
            item['report_timing'] = event.when
            item['recommendation'] = self._generate_recommendation(item)
        
        logger.info(f"High-probability earnings plays: {[x['symbol'] for x in beaters[:5]]}")
        
        return beaters
    
    def _generate_recommendation(self, item: dict) -> str:
        """Generate trading recommendation based on earnings data."""
        beat_rate = item['beat_rate']
        days_until = item['days_until_earnings']
        
        if beat_rate >= 80 and days_until <= 7:
            return "STRONG_BUY - High beat rate, earnings imminent"
        elif beat_rate >= 75 and days_until <= 14:
            return "BUY - Good beat history, earnings soon"
        elif beat_rate >= 70 and days_until <= 21:
            return "WATCH - Decent beat history, monitor"
        else:
            return "NEUTRAL"


def estimate_earnings_price_impact(historical: List[HistoricalEarnings]) -> dict:
    """
    Estimate expected price impact from earnings based on historical data.
    
    Returns:
    - avg_move_on_beat: Average % move when beats
    - avg_move_on_miss: Average % move when misses
    - upside_probability: Probability of positive move
    """
    if not historical:
        return {
            'avg_move_on_beat': 0.0,
            'avg_move_on_miss': 0.0,
            'upside_probability': 0.5
        }
    
    beats = [h for h in historical if h.eps_surprise > 0 and h.move_1d_pct is not None]
    misses = [h for h in historical if h.eps_surprise < 0 and h.move_1d_pct is not None]
    
    avg_beat_move = sum([h.move_1d_pct for h in beats]) / len(beats) if beats else 0.0
    avg_miss_move = sum([h.move_1d_pct for h in misses]) / len(misses) if misses else 0.0
    
    positive_moves = [h for h in historical if h.move_1d_pct and h.move_1d_pct > 0]
    upside_prob = len(positive_moves) / len(historical) if historical else 0.5
    
    return {
        'avg_move_on_beat': avg_beat_move,
        'avg_move_on_miss': avg_miss_move,
        'upside_probability': upside_prob,
        'total_beats': len(beats),
        'total_misses': len(misses)
    }
