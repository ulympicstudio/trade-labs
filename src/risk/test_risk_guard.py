from datetime import date
from src.risk.risk_guard import (
    RiskState,
    approve_new_trade,
    should_halt_trading,
    calc_max_open_risk_usd,
    calc_daily_max_loss_usd,
)

def test_open_risk_cap_blocks():
    """Test that exceeding open risk cap blocks new trades."""
    equity = 100_000
    max_open = calc_max_open_risk_usd(equity)  # 2% = 2000
    state = RiskState(day=date.today())

    # Already at cap, any new risk should fail
    status = approve_new_trade(state, equity_usd=equity, open_risk_usd=max_open, proposed_trade_risk_usd=1)
    assert status.allowed is False
    print("✓ Test open risk cap blocks passed")
    print(f"  Reason: {status.reason}")

def test_daily_halt_triggers():
    """Test that daily loss limit triggers halt."""
    equity = 100_000
    limit = calc_daily_max_loss_usd(equity)  # 1% = 1000
    state = RiskState(day=date.today())

    reason = should_halt_trading(state, equity_usd=equity, realized_pnl_usd=-limit, unrealized_pnl_usd=0)
    assert reason is not None
    print("✓ Test daily halt triggers passed")
    print(f"  Reason: {reason}")

def test_approval_passes_under_limits():
    """Test that trades are approved when under limits."""
    equity = 100_000
    state = RiskState(day=date.today())
    status = approve_new_trade(state, equity_usd=equity, open_risk_usd=0, proposed_trade_risk_usd=100)
    assert status.allowed is True
    print("✓ Test approval passes under limits passed")
    print(f"  Status: {status.reason}")


if __name__ == "__main__":
    test_open_risk_cap_blocks()
    test_daily_halt_triggers()
    test_approval_passes_under_limits()
    print("\n✓ All risk guard tests passed!")
