"""Interactive Brokers broker adapter (stub with placeholder implementations)."""


class IBBroker:
    """Stub for Interactive Brokers connection with placeholder data methods."""

    def connect(self):
        """Connect to IB."""
        # Placeholder: actual IB connection logic would go here
        pass

    def disconnect(self):
        """Disconnect from IB."""
        # Placeholder: actual IB cleanup logic would go here
        pass

    def get_last_price(self, symbol: str) -> float:
        """
        Fetch the last trade price for a symbol.
        
        Placeholder returning a realistic SPY price. 
        Real implementation would query IB API.
        """
        # Mock data for MVP testing
        if symbol == "SPY":
            return 502.5  # Realistic SPY price
        return 100.0

    def get_atr(self, symbol: str, period: int = 14) -> float:
        """
        Fetch the Average True Range for a symbol.
        
        Placeholder returning a realistic ATR. 
        Real implementation would calculate from historical bars.
        """
        # Mock data for MVP testing
        if symbol == "SPY":
            return 4.8  # Realistic SPY ATR
        return 2.0

    def get_account_equity(self) -> float:
        """
        Fetch the account NetLiquidation (total equity).
        
        Placeholder returning a realistic value. 
        Real implementation would query IB account details.
        """
        # Mock data for MVP testing - returning a typical trading account
        return 105_000.0

    def get_account_summary(self) -> dict:
        """Get full account details."""
        return {
            "NetLiquidation": self.get_account_equity(),
            "BuyingPower": self.get_account_equity() * 0.9,
        }
