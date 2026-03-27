# U.T.S. Live Promotion Checklist

This checklist MUST be fully satisfied before `TL_LIVE_TRADING` is set to `1`.
No exceptions. Review with full session log evidence before promoting.

---

## Hard Requirements

- [ ] Minimum **4 continuous weeks** of daily paper sessions completed
- [ ] Minimum **200 paper trades** executed (orders_filled, not just intents)
- [ ] Positive net expectancy after simulated slippage (TL_EXEC_SIM_FRICTION=true)
- [ ] Win rate >= 50% over trailing 100 trades
- [ ] Max single-session drawdown never exceeded configured daily loss limit
- [ ] Circuit breaker (kill switch) triggered and recovered cleanly at least once
- [ ] No unresolved arm crashes or silent failures in trailing 5 sessions
- [ ] Monitor arm heartbeat stable (no WATCHDOG warnings) for trailing 5 sessions
- [ ] Session logger writing clean JSON for every session in logs/

---

## Pre-Live Smoke Test (do this before first real $)

- [ ] Set max_positions = 1, risk_per_trade = $10 (minimum notional)
- [ ] Run 1 live session, confirm single order placed + filled in IBKR live account
- [ ] Confirm stop loss and trailing stop both appear in TWS order book
- [ ] Confirm monitor arm sends iMessage alert on fill
- [ ] Revert to paper immediately after smoke test passes

---

## Sign-Off

Date paper started   : 2026-03-25
Target promotion date: 2026-04-25 (earliest)
Actual promotion date: ___________
Signed off by        : ___________

---

> "Go slow to go fast. Every week of paper is insurance."
