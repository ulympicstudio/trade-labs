"""Interactive Brokers broker adapter — real ib_insync implementation."""

import logging

from ib_insync import IB, Stock

log = logging.getLogger("ib_broker")


class IBBroker:
    """Broker adapter wrapping the shared IB session from ib_session.py."""

    def _ib(self) -> IB:
        from src.broker.ib_session import get_ib
        return get_ib()

    def connect(self):
        """Ensure IB connection is active (delegates to session singleton)."""
        self._ib()

    def disconnect(self):
        """Disconnect the shared IB session."""
        try:
            ib = self._ib()
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    def get_last_price(self, symbol: str) -> float:
        """Fetch the last trade price for a symbol via IB market data."""
        ib = self._ib()
        c = Stock(symbol, "SMART", "USD")
        ticker = ib.reqMktData(c, "", False, False)
        ib.sleep(0.5)
        price = ticker.last or ticker.close or 0.0
        ib.cancelMktData(c)
        if price <= 0:
            raise ValueError(f"No price for {symbol}")
        return float(price)

    def get_atr(self, symbol: str, period: int = 14) -> float:
        """Calculate ATR from IB historical daily bars."""
        ib = self._ib()
        c = Stock(symbol, "SMART", "USD")
        bars = ib.reqHistoricalData(
            c, "", f"{period + 5} D", "1 day", "TRADES", False
        )
        if not bars or len(bars) < period:
            raise ValueError(f"Insufficient bars for ATR({period}) on {symbol}")
        highs = [b.high for b in bars[-period:]]
        lows = [b.low for b in bars[-period:]]
        closes = [b.close for b in bars[-(period + 1):-1]]
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i]), abs(lows[i] - closes[i]))
            for i in range(period)
        ]
        return round(sum(trs) / period, 4)

    def get_account_equity(self) -> float:
        """Fetch NetLiquidation from IB account values."""
        ib = self._ib()
        vals = {v.tag: v.value for v in ib.accountValues() if v.currency == "USD"}
        equity = float(vals.get("NetLiquidation", 0))
        if equity <= 0:
            raise ValueError("Cannot retrieve NetLiquidation from IB")
        return equity

    def get_account_summary(self) -> dict:
        """Get full account details from IB."""
        equity = self.get_account_equity()
        ib = self._ib()
        vals = {v.tag: v.value for v in ib.accountValues() if v.currency == "USD"}
        return {
            "NetLiquidation": equity,
            "BuyingPower": float(vals.get("BuyingPower", 0)),
        }
