"""
Quantitative Signal Scoring Engine
Uses hundreds of metrics to calculate probability scores for swing trades.
Combines momentum, mean reversion, volatility, volume, and market microstructure.
"""

import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Optional
from src.quant.technical_indicators import IndicatorResponse


@dataclass
class QuantScore:
    """Quantitative scoring result with probabilities."""
    symbol: str
    timestamp: str
    
    # Composite scores (0-100)
    total_score: float  # Overall probability score
    
    # Individual component scores
    momentum_score: float  # Trend following strength
    mean_reversion_score: float  # Oversold/overbought bounce potential
    volatility_score: float  # Risk-adjusted opportunity
    volume_score: float  # Liquidity and conviction
    microstructure_score: float  # Execution quality
    
    # Trade direction
    direction: str  # "LONG" or "SHORT"
    confidence: float  # 0-100, how confident in this trade
    
    # Entry/exit suggestions
    suggested_entry: Optional[float] = None
    suggested_stop: Optional[float] = None
    suggested_target: Optional[float] = None
    
    # Risk metrics
    expected_return_pct: Optional[float] = None  # Expected % gain
    risk_reward_ratio: Optional[float] = None  # R:R ratio
    
    # Supporting indicators
    key_signals: List[str] = None  # List of key signals detected
    
    def __post_init__(self):
        if self.key_signals is None:
            self.key_signals = []


class QuantScorer:
    """
    Quantitative scoring engine that analyzes hundreds of metrics
    to generate high-probability swing trade signals.
    """
    
    def __init__(self, 
                 momentum_weight: float = 0.3,
                 mean_reversion_weight: float = 0.25,
                 volatility_weight: float = 0.2,
                 volume_weight: float = 0.15,
                 microstructure_weight: float = 0.1):
        """
        Initialize with component weights (must sum to 1.0).
        """
        self.momentum_weight = momentum_weight
        self.mean_reversion_weight = mean_reversion_weight
        self.volatility_weight = volatility_weight
        self.volume_weight = volume_weight
        self.microstructure_weight = microstructure_weight
        
        # Validate weights sum to 1.0
        total = sum([momentum_weight, mean_reversion_weight, volatility_weight, 
                    volume_weight, microstructure_weight])
        assert abs(total - 1.0) < 0.01, f"Weights must sum to 1.0, got {total}"
    
    def score_momentum(self, indicators: IndicatorResponse) -> tuple[float, List[str]]:
        """
        Score momentum signals (0-100).
        Looks for strong trending behavior with confirmation.
        """
        score = 0
        signals = []
        
        # RSI momentum (30 points)
        if indicators.rsi_14:
            if 50 < indicators.rsi_14 < 70:  # Bullish momentum zone
                score += 15 * (indicators.rsi_14 - 50) / 20
                signals.append(f"RSI bullish momentum ({indicators.rsi_14:.1f})")
            elif 30 < indicators.rsi_14 < 50:  # Bearish but not oversold
                score += 15 * (50 - indicators.rsi_14) / 20
                signals.append(f"RSI bearish momentum ({indicators.rsi_14:.1f})")
        
        # MACD crossover (25 points)
        if indicators.macd_histogram:
            if indicators.macd_histogram > 0:
                score += min(25, abs(indicators.macd_histogram) * 10)
                signals.append("MACD positive crossover")
            else:
                score -= min(25, abs(indicators.macd_histogram) * 10)
        
        # Moving average alignment (25 points)
        if all([indicators.ema_9, indicators.ema_21, indicators.ema_50]):
            # Bullish: 9 > 21 > 50
            if indicators.ema_9 > indicators.ema_21 > indicators.ema_50:
                score += 25
                signals.append("Bullish EMA alignment (9>21>50)")
            # Bearish: 9 < 21 < 50
            elif indicators.ema_9 < indicators.ema_21 < indicators.ema_50:
                score += 25
                signals.append("Bearish EMA alignment (9<21<50)")
        
        # Recent price action (20 points)
        if indicators.return_5d:
            # Strong 5-day move
            if abs(indicators.return_5d) > 5:
                score += min(20, abs(indicators.return_5d) * 2)
                direction = "upward" if indicators.return_5d > 0 else "downward"
                signals.append(f"Strong {direction} momentum ({indicators.return_5d:+.1f}%)")
        
        return min(100, max(0, score)), signals
    
    def score_mean_reversion(self, indicators: IndicatorResponse) -> tuple[float, List[str]]:
        """
        Score mean reversion opportunities (0-100).
        Looks for oversold/overbought conditions with reversal potential.
        """
        score = 0
        signals = []
        
        # RSI extremes (30 points)
        if indicators.rsi_14:
            if indicators.rsi_14 < 30:  # Oversold
                score += (30 - indicators.rsi_14) * 1.5
                signals.append(f"RSI oversold ({indicators.rsi_14:.1f})")
            elif indicators.rsi_14 > 70:  # Overbought
                score += (indicators.rsi_14 - 70) * 1.5
                signals.append(f"RSI overbought ({indicators.rsi_14:.1f})")
        
        # Bollinger Band position (30 points)
        if indicators.bollinger_position is not None:
            # Near lower band (oversold)
            if indicators.bollinger_position < 0.2:
                score += (0.2 - indicators.bollinger_position) * 150
                signals.append(f"Price at lower Bollinger Band ({indicators.bollinger_position:.2f})")
            # Near upper band (overbought)
            elif indicators.bollinger_position > 0.8:
                score += (indicators.bollinger_position - 0.8) * 150
                signals.append(f"Price at upper Bollinger Band ({indicators.bollinger_position:.2f})")
        
        # Z-score extremes (25 points)
        if indicators.zscore_20:
            if abs(indicators.zscore_20) > 2:  # 2+ standard deviations
                score += min(25, (abs(indicators.zscore_20) - 2) * 10)
                direction = "below" if indicators.zscore_20 < -2 else "above"
                signals.append(f"Price {abs(indicators.zscore_20):.1f}σ {direction} mean")
        
        # Stochastic extremes (15 points)
        if indicators.stochastic_k:
            if indicators.stochastic_k < 20:  # Oversold
                score += (20 - indicators.stochastic_k) * 0.75
                signals.append(f"Stochastic oversold ({indicators.stochastic_k:.1f})")
            elif indicators.stochastic_k > 80:  # Overbought
                score += (indicators.stochastic_k - 80) * 0.75
                signals.append(f"Stochastic overbought ({indicators.stochastic_k:.1f})")
        
        return min(100, max(0, score)), signals
    
    def score_volatility(self, indicators: IndicatorResponse) -> tuple[float, List[str]]:
        """
        Score volatility environment (0-100).
        Prefers high volatility for swing trades (more opportunity).
        """
        score = 0
        signals = []
        
        # ATR relative to price (40 points) - higher is better for swings
        if indicators.atr_14 and indicators.close:
            atr_pct = (indicators.atr_14 / indicators.close) * 100
            if atr_pct > 3:  # >3% daily range
                score += min(40, atr_pct * 8)
                signals.append(f"High ATR ({atr_pct:.1f}% of price)")
        
        # Bollinger Band width (30 points) - expansion means opportunity
        if indicators.bollinger_width and indicators.bollinger_middle:
            width_pct = (indicators.bollinger_width / indicators.bollinger_middle) * 100
            if width_pct > 5:  # Wide bands
                score += min(30, width_pct * 4)
                signals.append(f"Wide Bollinger Bands ({width_pct:.1f}%)")
        
        # Recent volatility (30 points)
        if indicators.volatility_20d:
            if 20 < indicators.volatility_20d < 60:  # Sweet spot for swing trades
                score += 30
                signals.append(f"Optimal volatility ({indicators.volatility_20d:.1f}%)")
            elif indicators.volatility_20d > 60:  # High but risky
                score += 20
                signals.append(f"High volatility ({indicators.volatility_20d:.1f}%)")
        
        return min(100, max(0, score)), signals
    
    def score_volume(self, indicators: IndicatorResponse) -> tuple[float, List[str]]:
        """
        Score volume patterns (0-100).
        Looks for conviction and institutional participation.
        """
        score = 0
        signals = []
        
        # Volume ratio (40 points)
        if indicators.volume_ratio:
            if indicators.volume_ratio > 1.5:  # Above-average volume
                score += min(40, (indicators.volume_ratio - 1) * 40)
                signals.append(f"High volume ({indicators.volume_ratio:.1f}x avg)")
        
        # Volume spikes (30 points)
        if indicators.recent_volume_spikes > 0:
            score += min(30, indicators.recent_volume_spikes * 10)
            signals.append(f"{indicators.recent_volume_spikes} recent volume spikes")
        
        # Chaikin Money Flow (30 points)
        if indicators.cmf:
            if abs(indicators.cmf) > 0.1:  # Strong buying/selling pressure
                score += min(30, abs(indicators.cmf) * 150)
                flow_type = "buying" if indicators.cmf > 0 else "selling"
                signals.append(f"Strong {flow_type} pressure (CMF {indicators.cmf:.2f})")
        
        return min(100, max(0, score)), signals
    
    def score_microstructure(self, indicators: IndicatorResponse) -> tuple[float, List[str]]:
        """
        Score execution quality (0-100).
        Tight spreads and good liquidity enable profitable swing trading.
        """
        score = 50  # Default to neutral
        signals = []
        
        # Bid-ask spread (100 points - very important)
        if indicators.bid_ask_spread_pct is not None:
            if indicators.bid_ask_spread_pct < 0.1:  # <0.1% spread (excellent)
                score = 100
                signals.append(f"Tight spread ({indicators.bid_ask_spread_pct:.3f}%)")
            elif indicators.bid_ask_spread_pct < 0.2:  # <0.2% spread (good)
                score = 80
                signals.append(f"Good spread ({indicators.bid_ask_spread_pct:.3f}%)")
            elif indicators.bid_ask_spread_pct > 0.5:  # >0.5% spread (poor)
                score = 20
                signals.append(f"Wide spread ({indicators.bid_ask_spread_pct:.3f}%)")
        
        return min(100, max(0, score)), signals
    
    def determine_direction(self, momentum_score: float, mean_reversion_score: float,
                           indicators: IndicatorResponse) -> str:
        """
        Determine trade direction (LONG or SHORT) based on dominant signals.
        """
        # Momentum-driven
        if momentum_score > mean_reversion_score:
            # Look at trend indicators
            if indicators.rsi_14 and indicators.rsi_14 > 50:
                return "LONG"
            elif indicators.macd and indicators.macd > 0:
                return "LONG"
            else:
                return "SHORT"
        
        # Mean reversion-driven
        else:
            # Oversold = LONG, Overbought = SHORT
            if indicators.rsi_14 and indicators.rsi_14 < 40:
                return "LONG"
            elif indicators.bollinger_position and indicators.bollinger_position < 0.3:
                return "LONG"
            elif indicators.rsi_14 and indicators.rsi_14 > 60:
                return "SHORT"
            elif indicators.bollinger_position and indicators.bollinger_position > 0.7:
                return "SHORT"
            else:
                return "LONG"  # Default
    
    def calculate_entry_exit(self, indicators: IndicatorResponse, direction: str,
                            current_price: float) -> tuple[float, float, float]:
        """
        Calculate suggested entry, stop loss, and profit target.
        Uses ATR for stop placement and risk:reward for targets.
        """
        atr = indicators.atr_14 or (current_price * 0.02)  # Default 2% if no ATR
        
        if direction == "LONG":
            # Entry: slightly below current for limit order
            entry = current_price * 0.998
            
            # Stop: 1.5-2 ATRs below entry
            stop = entry - (atr * 1.8)
            
            # Target: 2-3x risk (2.5x default)
            risk = entry - stop
            target = entry + (risk * 2.5)
        
        else:  # SHORT
            entry = current_price * 1.002
            stop = entry + (atr * 1.8)
            risk = stop - entry
            target = entry - (risk * 2.5)
        
        return float(entry), float(stop), float(target)
    
    def calculate_score(self, indicators: IndicatorResponse, 
                       current_price: float) -> QuantScore:
        """
        Calculate comprehensive quantitative score from all indicators.
        Returns QuantScore with probability estimate and trade details.
        """
        # Score each component
        momentum_score, momentum_signals = self.score_momentum(indicators)
        mean_reversion_score, reversion_signals = self.score_mean_reversion(indicators)
        volatility_score, volatility_signals = self.score_volatility(indicators)
        volume_score, volume_signals = self.score_volume(indicators)
        microstructure_score, micro_signals = self.score_microstructure(indicators)
        
        # Weighted composite score
        total_score = (
            momentum_score * self.momentum_weight +
            mean_reversion_score * self.mean_reversion_weight +
            volatility_score * self.volatility_weight +
            volume_score * self.volume_weight +
            microstructure_score * self.microstructure_weight
        )
        
        # Determine direction
        direction = self.determine_direction(momentum_score, mean_reversion_score, indicators)
        
        # Calculate entry/exit levels
        entry, stop, target = self.calculate_entry_exit(indicators, direction, current_price)
        
        # Risk metrics
        risk_amount = abs(entry - stop)
        reward_amount = abs(target - entry)
        risk_reward_ratio = reward_amount / risk_amount if risk_amount > 0 else 0
        
        expected_return_pct = ((target - entry) / entry) * 100 if direction == "LONG" else \
                             ((entry - target) / entry) * 100
        
        # Confidence: based on signal agreement and score
        all_signals = momentum_signals + reversion_signals + volatility_signals + \
                     volume_signals + micro_signals
        
        confidence = min(100, total_score * 0.7 + len(all_signals) * 3)
        
        return QuantScore(
            symbol=indicators.symbol,
            timestamp=indicators.timestamp,
            total_score=round(total_score, 2),
            momentum_score=round(momentum_score, 2),
            mean_reversion_score=round(mean_reversion_score, 2),
            volatility_score=round(volatility_score, 2),
            volume_score=round(volume_score, 2),
            microstructure_score=round(microstructure_score, 2),
            direction=direction,
            confidence=round(confidence, 2),
            suggested_entry=round(entry, 2),
            suggested_stop=round(stop, 2),
            suggested_target=round(target, 2),
            expected_return_pct=round(expected_return_pct, 2),
            risk_reward_ratio=round(risk_reward_ratio, 2),
            key_signals=all_signals[:10]  # Top 10 signals
        )
    
    def rank_opportunities(self, scores: List[QuantScore], top_n: int = 50) -> List[QuantScore]:
        """
        Rank trading opportunities by total score and confidence.
        Returns top N candidates.
        """
        # Sort by composite of score and confidence
        ranked = sorted(
            scores,
            key=lambda s: (s.total_score * 0.7 + s.confidence * 0.3),
            reverse=True
        )
        
        return ranked[:top_n]


def calculate_portfolio_correlation(scores: List[QuantScore]) -> Dict[str, float]:
    """
    Calculate correlation between selected symbols for diversification.
    (Placeholder - needs historical price data)
    """
    # TODO: Implement with actual price correlation
    return {"avg_correlation": 0.3, "max_correlation": 0.7}


def optimize_position_allocation(scores: List[QuantScore], 
                                 total_capital: float,
                                 max_positions: int = 20) -> Dict[str, float]:
    """
    Allocate capital across opportunities based on scores and risk.
    Uses Kelly Criterion-inspired approach.
    """
    allocations = {}
    
    # Filter to top positions
    top_scores = sorted(scores, key=lambda s: s.total_score, reverse=True)[:max_positions]
    
    # Calculate weights based on confidence and risk:reward
    total_weight = sum(s.confidence * s.risk_reward_ratio for s in top_scores)
    
    for score in top_scores:
        weight = (score.confidence * score.risk_reward_ratio) / total_weight
        allocation = total_capital * weight
        allocations[score.symbol] = round(allocation, 2)
    
    return allocations
