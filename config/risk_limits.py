# config/risk_limits.py

# Portfolio-level limits
MAX_OPEN_RISK_PCT = 0.02     # 2% of equity at risk across all open positions
MAX_TRADES_PER_DAY = 10      # safety cap (you can raise later)

# Daily shutdown limits
DAILY_MAX_LOSS_PCT = 0.01    # 1% equity daily loss shuts down new entries

# Optional: per-trade cap (extra safety)
MAX_RISK_PER_TRADE_PCT = 0.005  # 0.5% default, can be overridden by scoring later
