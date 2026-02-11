from src.risk.position_sizing import calculate_position_size, PositionSizeResult


def test_position_size_with_atr():
    """Test position sizing with ATR-derived stop loss.
    
    100k account
    0.5% risk = $500 allowed loss
    ATR = 5
    2x ATR = 10 stop distance
    Entry price = 500, Stop = 490
    Risk per share = $10
    Max allowed loss = $500
    Shares = 50
    """
    result = calculate_position_size(
        account_equity=100_000,
        risk_percent=0.005,
        entry_price=500,
        atr=5,
        atr_multiplier=2.0,
    )
    
    assert result.entry_price == 500
    assert result.stop_price == 490
    assert result.risk_per_share == 10
    assert result.shares == 50
    assert result.total_risk == 500
    print("✓ Test with ATR passed")
    print(f"  Entry: ${result.entry_price}, Stop: ${result.stop_price}")
    print(f"  Risk per share: ${result.risk_per_share}")
    print(f"  Shares: {result.shares}, Total risk: ${result.total_risk}")


def test_position_size_with_explicit_stop():
    """Test position sizing with explicit stop price."""
    result = calculate_position_size(
        account_equity=100_000,
        risk_percent=0.005,
        entry_price=500,
        stop_price=490,
    )
    
    assert result.entry_price == 500
    assert result.stop_price == 490
    assert result.risk_per_share == 10
    assert result.shares == 50
    assert result.total_risk == 500
    print("✓ Test with explicit stop passed")
    print(f"  Entry: ${result.entry_price}, Stop: ${result.stop_price}")
    print(f"  Risk per share: ${result.risk_per_share}")
    print(f"  Shares: {result.shares}, Total risk: ${result.total_risk}")


def test_position_size_different_risk_percent():
    """Test position sizing with different risk percentage."""
    result = calculate_position_size(
        account_equity=100_000,
        risk_percent=0.01,  # 1% risk
        entry_price=100,
        atr=2,
        atr_multiplier=2.0,
    )
    
    # Stop = 100 - (2 * 2) = 96
    # Risk per share = 100 - 96 = 4
    # Max risk = 100,000 * 0.01 = $1000
    # Shares = int(1000 // 4) = 250
    assert result.stop_price == 96
    assert result.risk_per_share == 4
    assert result.shares == 250
    assert result.total_risk == 1000
    print("✓ Test with 1% risk passed")
    print(f"  Shares: {result.shares}, Total risk: ${result.total_risk}")


if __name__ == "__main__":
    test_position_size_with_atr()
    test_position_size_with_explicit_stop()
    test_position_size_different_risk_percent()
    print("\n✓ All tests passed!")
