# config/universe_filter.py
# Shared universe filter configuration used by scanner and live loop

# Only common stocks
ALLOWED_SEC_TYPES = {"STK"}
ALLOWED_EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}

# Always allow these (even if they look like ETFs)
STOCK_ALLOWLIST = {"SPY", "QQQ"}

# Always block (commodity ETFs, leveraged ETFs, crypto trusts, etc.)
STOCK_BLOCKLIST = {"UNG", "SLV", "KOLD", "BITO"}

# Keywords in longName that indicate product is not a tradeable stock
ETF_KEYWORDS = {"ETF", "ETN", "FUND", "TRUST", "INDEX", "NOTE", "NOTES", "SECURITIES"}
