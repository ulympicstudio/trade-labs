"""
Historical Data Manager for Backtesting
Fetches and caches historical market data from Interactive Brokers.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
import pandas as pd
from ib_insync import IB, Stock, util
import pickle

logger = logging.getLogger(__name__)


class HistoricalDataManager:
    """
    Manages historical price data for backtesting.
    
    Features:
    - Fetches data from IB
    - Caches to disk (avoid re-fetching)
    - Handles multiple symbols
    - Data validation
    """
    
    def __init__(self, ib: IB, cache_dir: str = "backtest_cache"):
        """
        Initialize data manager.
        
        Args:
            ib: Connected IB instance
            cache_dir: Directory for cached data
        """
        self.ib = ib
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        
        logger.info(f"HistoricalDataManager initialized (cache: {cache_dir})")
    
    def get_historical_bars(self, symbol: str,
                           start_date: datetime,
                           end_date: datetime,
                           bar_size: str = "1 day",
                           use_cache: bool = True) -> Optional[pd.DataFrame]:
        """
        Get historical price data for a symbol.
        
        Args:
            symbol: Stock symbol
            start_date: Start date
            end_date: End date
            bar_size: Bar size (1 day, 1 hour, etc.)
            use_cache: Use cached data if available
            
        Returns:
            DataFrame with OHLCV data or None
        """
        cache_file = self._get_cache_file(symbol, start_date, end_date, bar_size)
        
        # Check cache first
        if use_cache and cache_file.exists():
            try:
                df = pd.read_pickle(cache_file)
                logger.info(f"Loaded {symbol} from cache ({len(df)} bars)")
                return df
            except Exception as e:
                logger.warning(f"Cache read failed for {symbol}: {e}")
        
        # Fetch from IB
        try:
            contract = Stock(symbol, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)
            
            # Calculate duration
            duration_days = (end_date - start_date).days
            duration_str = f"{duration_days} D"
            
            # Request historical data
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime=end_date,
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            )
            
            if not bars:
                logger.warning(f"No data received for {symbol}")
                return None
            
            # Convert to DataFrame
            df = util.df(bars)
            
            if df.empty:
                logger.warning(f"Empty DataFrame for {symbol}")
                return None
            
            # Add symbol column
            df['symbol'] = symbol
            
            # Save to cache
            try:
                df.to_pickle(cache_file)
                logger.info(f"Cached {symbol} ({len(df)} bars)")
            except Exception as e:
                logger.warning(f"Cache write failed for {symbol}: {e}")
            
            logger.info(f"Fetched {symbol}: {len(df)} bars from {df.index[0]} to {df.index[-1]}")
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch {symbol}: {e}")
            return None
    
    def get_multiple_symbols(self, symbols: List[str],
                            start_date: datetime,
                            end_date: datetime,
                            bar_size: str = "1 day",
                            use_cache: bool = True) -> Dict[str, pd.DataFrame]:
        """
        Get historical data for multiple symbols.
        
        Args:
            symbols: List of symbols
            start_date: Start date
            end_date: End date
            bar_size: Bar size
            use_cache: Use cached data
            
        Returns:
            Dict mapping symbol -> DataFrame
        """
        data = {}
        
        logger.info(f"Fetching historical data for {len(symbols)} symbols...")
        
        for i, symbol in enumerate(symbols, 1):
            logger.info(f"[{i}/{len(symbols)}] Fetching {symbol}...")
            
            df = self.get_historical_bars(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                bar_size=bar_size,
                use_cache=use_cache
            )
            
            if df is not None:
                data[symbol] = df
            
            # Rate limiting - IB has request limits
            if i < len(symbols):
                util.sleep(0.1)  # Small delay between requests
        
        logger.info(f"Successfully fetched {len(data)}/{len(symbols)} symbols")
        return data
    
    def _get_cache_file(self, symbol: str, start_date: datetime,
                       end_date: datetime, bar_size: str) -> Path:
        """Generate cache filename."""
        start_str = start_date.strftime('%Y%m%d')
        end_str = end_date.strftime('%Y%m%d')
        bar_str = bar_size.replace(' ', '_')
        filename = f"{symbol}_{start_str}_{end_str}_{bar_str}.pkl"
        return self.cache_dir / filename
    
    def clear_cache(self, symbol: Optional[str] = None):
        """
        Clear cached data.
        
        Args:
            symbol: Clear specific symbol, or all if None
        """
        if symbol:
            pattern = f"{symbol}_*.pkl"
            files = list(self.cache_dir.glob(pattern))
        else:
            files = list(self.cache_dir.glob("*.pkl"))
        
        for file in files:
            file.unlink()
        
        logger.info(f"Cleared {len(files)} cache files")
    
    def validate_data(self, df: pd.DataFrame) -> bool:
        """
        Validate historical data quality.
        
        Checks:
        - Required columns present
        - No missing values
        - Date is sorted
        - Prices are positive
        
        Args:
            df: DataFrame to validate
            
        Returns:
            True if valid
        """
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        
        # Check columns
        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            logger.error(f"Missing columns: {missing}")
            return False
        
        # Check for NaN
        if df[required_cols].isnull().any().any():
            logger.error("Data contains NaN values")
            return False
        
        # Check date sorted
        if not df.index.is_monotonic_increasing:
            logger.error("Data is not sorted by date")
            return False
        
        # Check positive prices
        price_cols = ['open', 'high', 'low', 'close']
        if (df[price_cols] <= 0).any().any():
            logger.error("Data contains non-positive prices")
            return False
        
        # Check high >= low
        if (df['high'] < df['low']).any():
            logger.error("Data has high < low")
            return False
        
        return True
    
    def get_price_on_date(self, symbol: str, date: datetime) -> Optional[Dict]:
        """
        Get OHLCV for a specific date.
        
        Args:
            symbol: Stock symbol
            date: Date to retrieve
            
        Returns:
            Dict with OHLCV or None
        """
        # Fetch data around that date (1 week buffer)
        start = date - timedelta(days=7)
        end = date + timedelta(days=1)
        
        df = self.get_historical_bars(symbol, start, end)
        
        if df is None:
            return None
        
        # Find exact date
        date_str = date.strftime('%Y-%m-%d')
        matching = df[df.index.strftime('%Y-%m-%d') == date_str]
        
        if matching.empty:
            logger.warning(f"No data for {symbol} on {date_str}")
            return None
        
        row = matching.iloc[0]
        return {
            'open': row['open'],
            'high': row['high'],
            'low': row['low'],
            'close': row['close'],
            'volume': row['volume']
        }
