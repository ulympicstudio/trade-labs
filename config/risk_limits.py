# config/risk_limits.py

# Portfolio-level limits
MAX_OPEN_RISK_PCT = 0.02     # 2% of equity at risk across all open positions (base mode)
MAX_TRADES_PER_DAY = 10      # safety cap (you can raise later)

# Daily shutdown limits
DAILY_MAX_LOSS_PCT = 0.03    # 3% equity daily loss shuts down new entries

# Optional: per-trade cap (extra safety)
MAX_RISK_PER_TRADE_PCT = 0.005  # 0.5% default, can be overridden by scoring later

# ── Phase 2: Signal Quality Defaults ──────────────────────────────────────

# Unified score (0.60 * CatalystScore + 0.40 * QuantScore)
MIN_UNIFIED_SCORE = 70

# Hyper-swing universe gates
MIN_ADV20_DOLLARS = 25_000_000   # Minimum 20-day avg daily $ volume
MIN_ATR_PCT       = 0.008        # ATR / price >= 0.8%
MIN_VOLUME_ACCEL  = 1.3          # Last 15m volume / avg prior 8x15m buckets
MIN_RS_VS_SPY     = 0.0025       # RS_30m delta-return vs SPY (0.25%)

# Price band (exceptions via PRICE_MAX_ALLOWLIST)
PRICE_MIN = 2.0
PRICE_MAX = 250

# Large-cap exceptions allowed above PRICE_MAX
PRICE_MAX_ALLOWLIST = {"NVDA", "META", "AAPL", "TSLA", "PLTR", "PANW", "MSFT", "AMZN"}

