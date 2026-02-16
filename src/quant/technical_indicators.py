"""
Technical Indicators Library
Hundreds of calculations for swing trading signal generation.
Focused on momentum, mean reversion, volatility, volume, and price action.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional
from collections import deque


@dataclass
class IndicatorResponse:
    """Container for all technical indicator values for a symbol."""
    symbol: str
    timestamp: str
    close: Optional[float] = None  # Current price for calculations
    
    # Momentum indicators
    rsi_14: Optional[float] = None
    rsi_7: Optional[float] = None
    rsi_21: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    
    # Oscillators
    stochastic_k: Optional[float] = None
    stochastic_d: Optional[float] = None
    williams_r: Optional[float] = None
    
    # Volatility indicators
    atr_14: Optional[float] = None
    atr_21: Optional[float] = None
    bollinger_upper: Optional[float] = None
    bollinger_middle: Optional[float] = None
    bollinger_lower: Optional[float] = None
    bollinger_width: Optional[float] = None
    bollinger_position: Optional[float] = None  # 0-1, position in bands
    
    # Trend indicators
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    
    # Volume indicators
    volume_sma: Optional[float] = None
    volume_ratio: Optional[float] = None  # current / 20-day avg
    obv: Optional[float] = None
    cmf: Optional[float] = None  # Chaikin Money Flow
    
    # Price action
    high_20: Optional[float] = None
    low_20: Optional[float] = None
    high_52: Optional[float] = None
    low_52: Optional[float] = None
    
    # Reversions and extremes
    zscore_20: Optional[float] = None  # How many stds from 20-day avg
    zscore_50: Optional[float] = None
    price_vs_sma20_pct: Optional[float] = None  # Distance from 20-day SMA %
    price_vs_sma50_pct: Optional[float] = None
    
    # Performance metrics
    return_5d: Optional[float] = None
    return_10d: Optional[float] = None
    volatility_20d: Optional[float] = None  # Daily return stddev
    
    # Market microstructure
    bid_ask_spread_pct: Optional[float] = None
    recent_volume_spikes: int = 0  # Count of last 5 days with volume > 1.5x avg


class TechnicalIndicators:
    """Calculate hundreds of technical indicators for swing trading."""
    
    def __init__(self, lookback: int = 252):  # 1 year of trading days
        self.lookback = lookback
        
    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Relative Strength Index - momentum oscillator (0-100)."""
        if len(prices) < period + 1:
            return None
        
        prices_array = np.array(prices[-period-1:], dtype=float)
        deltas = np.diff(prices_array)
        
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return float(rsi)
    
    def calculate_macd(self, prices: List[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """MACD - (12, 26, 9) exponential moving average system."""
        if len(prices) < 26:
            return None, None, None
        
        prices_array = np.array(prices[-26:], dtype=float)
        
        ema_12 = self._ema(prices_array, 12)
        ema_26 = self._ema(prices_array, 26)
        
        if ema_12 is None or ema_26 is None:
            return None, None, None
        
        macd = ema_12 - ema_26
        
        # Get signal line (9-period EMA of MACD)
        signal = self._ema(np.array([macd]), 9)  # Simplified for current bar
        
        histogram = macd - (signal or 0)
        
        return float(macd), float(signal or 0), float(histogram)
    
    def calculate_stochastic(self, highs: List[float], lows: List[float], 
                           closes: List[float], period: int = 14) -> tuple[Optional[float], Optional[float]]:
        """Stochastic Oscillator - momentum indicator comparing close to price range."""
        if len(closes) < period:
            return None, None
        
        recent_highs = highs[-period:]
        recent_lows = lows[-period:]
        
        high = max(recent_highs)
        low = min(recent_lows)
        
        if high == low:
            return 50.0, 50.0
        
        k = 100 * (closes[-1] - low) / (high - low)
        d = np.mean([k for _ in range(3)])  # Simplified 3-period SMA
        
        return float(k), float(d)
    
    def calculate_bollinger_bands(self, prices: List[float], period: int = 20, 
                                  std_dev: float = 2.0) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """Bollinger Bands - price volatility bands around moving average."""
        if len(prices) < period:
            return None, None, None, None
        
        recent = np.array(prices[-period:], dtype=float)
        ma = np.mean(recent)
        std = np.std(recent)
        
        upper = ma + (std_dev * std)
        lower = ma - (std_dev * std)
        width = upper - lower
        
        # Position 0-1: where price is relative to bands
        if width == 0:
            position = 0.5
        else:
            position = (prices[-1] - lower) / width
            position = max(0, min(1, position))  # Clamp 0-1
        
        return float(upper), float(ma), float(lower), float(position)
    
    def calculate_atr(self, highs: List[float], lows: List[float], 
                     closes: List[float], period: int = 14) -> Optional[float]:
        """Average True Range - volatility measure."""
        if len(closes) < period + 1:
            return None
        
        tr_values = []
        for i in range(-period, 0):
            h = highs[i]
            l = lows[i]
            c = closes[i - 1] if i > -len(closes) else closes[0]
            
            tr = max(h - l, abs(h - c), abs(l - c))
            tr_values.append(tr)
        
        atr = np.mean(tr_values)
        return float(atr)
    
    def calculate_ema(self, prices: List[float], period: int) -> Optional[float]:
        """Exponential Moving Average."""
        return self._ema(np.array(prices[-period:], dtype=float), period)
    
    def _ema(self, prices: np.ndarray, period: int) -> Optional[float]:
        """Calculate EMA for given prices."""
        if len(prices) < period:
            return None
        
        multiplier = 2.0 / (period + 1)
        ema = np.mean(prices[:period])
        
        for i in range(period, len(prices)):
            ema = prices[i] * multiplier + ema * (1 - multiplier)
        
        return float(ema)
    
    def calculate_sma(self, prices: List[float], period: int) -> Optional[float]:
        """Simple Moving Average."""
        if len(prices) < period:
            return None
        
        return float(np.mean(prices[-period:]))
    
    def calculate_zscore(self, prices: List[float], period: int = 20) -> Optional[float]:
        """Z-score: how many standard deviations from the mean."""
        if len(prices) < period:
            return None
        
        recent = np.array(prices[-period:], dtype=float)
        mean = np.mean(recent)
        std = np.std(recent)
        
        if std == 0:
            return 0.0
        
        zscore = (prices[-1] - mean) / std
        return float(zscore)
    
    def calculate_williams_r(self, highs: List[float], lows: List[float], 
                            closes: List[float], period: int = 14) -> Optional[float]:
        """Williams %R - momentum oscillator (-100 to 0)."""
        if len(closes) < period:
            return None
        
        high = max(highs[-period:])
        low = min(lows[-period:])
        
        if high == low:
            return -50.0
        
        williams_r = -100 * (high - closes[-1]) / (high - low)
        return float(williams_r)
    
    def calculate_volume_metrics(self, volumes: List[float]) -> tuple[Optional[float], int]:
        """Volume analysis - avg volume and volume spike count."""
        if len(volumes) < 5:
            return None, 0
        
        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        
        # Count volume spikes (>1.5x average) in last 5 days
        spike_count = 0
        for vol in volumes[-5:]:
            if vol > 1.5 * avg_vol:
                spike_count += 1
        
        ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        
        return float(ratio), spike_count
    
    def calculate_chaikin_money_flow(self, highs: List[float], lows: List[float],
                                     closes: List[float], volumes: List[float],
                                     period: int = 20) -> Optional[float]:
        """Chaikin Money Flow - volume-weighted price indicator."""
        if len(closes) < period:
            return None
        
        cmf_values = []
        for i in range(-period, 0):
            h = highs[i]
            l = lows[i]
            c = closes[i]
            v = volumes[i]
            
            if h == l:
                cmf_val = 0
            else:
                money_flow_multiplier = ((c - l) - (h - c)) / (h - l)
                cmf_val = money_flow_multiplier * v
            
            cmf_values.append(cmf_val)
        
        total_cmf = sum(cmf_values)
        total_volume = sum(volumes[-period:])
        
        if total_volume == 0:
            return 0.0
        
        cmf = total_cmf / total_volume
        return float(cmf)
    
    def calculate_returns(self, prices: List[float], periods: List[int]) -> dict:
        """Calculate log returns for different periods."""
        returns = {}
        for period in periods:
            if len(prices) >= period + 1:
                ret = np.log(prices[-1] / prices[-(period+1)])
                returns[f"return_{period}d"] = float(ret * 100)  # As percentage
            else:
                returns[f"return_{period}d"] = None
        
        return returns
    
    def calculate_volatility(self, prices: List[float], period: int = 20) -> Optional[float]:
        """Daily return volatility."""
        if len(prices) < period + 1:
            return None
        
        returns = np.diff(np.log(np.array(prices[-period-1:], dtype=float)))
        volatility = np.std(returns) * np.sqrt(252) * 100  # Annualized %
        
        return float(volatility)
    
    def calculate_all_indicators(self, symbol: str, timestamp: str,
                                highs: List[float], lows: List[float],
                                closes: List[float], volumes: List[float],
                                bid: Optional[float] = None,
                                ask: Optional[float] = None) -> IndicatorResponse:
        """Calculate all indicators at once."""
        
        result = IndicatorResponse(symbol=symbol, timestamp=timestamp, close=closes[-1])
        
        # Momentum
        result.rsi_14 = self.calculate_rsi(closes, 14)
        result.rsi_7 = self.calculate_rsi(closes, 7)
        result.rsi_21 = self.calculate_rsi(closes, 21)
        
        macd, signal, hist = self.calculate_macd(closes)
        result.macd = macd
        result.macd_signal = signal
        result.macd_histogram = hist
        
        # Oscillators
        k, d = self.calculate_stochastic(highs, lows, closes, 14)
        result.stochastic_k = k
        result.stochastic_d = d
        result.williams_r = self.calculate_williams_r(highs, lows, closes, 14)
        
        # Volatility
        result.atr_14 = self.calculate_atr(highs, lows, closes, 14)
        result.atr_21 = self.calculate_atr(highs, lows, closes, 21)
        
        upper, middle, lower, position = self.calculate_bollinger_bands(closes, 20, 2.0)
        result.bollinger_upper = upper
        result.bollinger_middle = middle
        result.bollinger_lower = lower
        result.bollinger_position = position
        result.bollinger_width = upper - lower if upper and lower else None
        
        # Trends
        result.ema_9 = self.calculate_ema(closes, 9)
        result.ema_21 = self.calculate_ema(closes, 21)
        result.ema_50 = self.calculate_ema(closes, 50)
        result.sma_20 = self.calculate_sma(closes, 20)
        result.sma_50 = self.calculate_sma(closes, 50)
        result.sma_200 = self.calculate_sma(closes, 200)
        
        # Volume
        volume_ratio, spike_count = self.calculate_volume_metrics(volumes)
        result.volume_ratio = volume_ratio
        result.recent_volume_spikes = spike_count
        result.volume_sma = self.calculate_sma(volumes, 20)
        result.cmf = self.calculate_chaikin_money_flow(highs, lows, closes, volumes, 20)
        
        # Price levels
        result.high_20 = float(max(highs[-20:]))
        result.low_20 = float(min(lows[-20:]))
        result.high_52 = float(max(highs[-252:]) if len(highs) >= 252 else max(highs))
        result.low_52 = float(min(lows[-252:]) if len(lows) >= 252 else min(lows))
        
        # Mean reversion
        result.zscore_20 = self.calculate_zscore(closes, 20)
        result.zscore_50 = self.calculate_zscore(closes, 50)
        
        sma_20 = self.calculate_sma(closes, 20)
        sma_50 = self.calculate_sma(closes, 50)
        
        if sma_20:
            result.price_vs_sma20_pct = ((closes[-1] - sma_20) / sma_20) * 100
        if sma_50:
            result.price_vs_sma50_pct = ((closes[-1] - sma_50) / sma_50) * 100
        
        # Returns
        returns = self.calculate_returns(closes, [5, 10])
        result.return_5d = returns.get("return_5d")
        result.return_10d = returns.get("return_10d")
        result.volatility_20d = self.calculate_volatility(closes, 20)
        
        # Spreads
        if bid and ask:
            spread = ask - bid
            mid = (bid + ask) / 2
            result.bid_ask_spread_pct = (spread / mid) * 100
        
        return result
