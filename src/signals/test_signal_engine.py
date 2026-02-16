import os

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.contracts.trade_intent import TradeIntent
from src.execution.pipeline import execute_trade_intent_paper

from src.data.ib_market_data import (
    connect_ib,
    get_spy_contract,
    get_history_bars,
    get_recent_price_from_history,
    get_account_equity_usd,
)
from src.indicators.atr import compute_atr
from src.risk.open_risk import estimate_open_risk_usd


def main():
    # Always paper mode
    os.environ["TRADE_LABS_MODE"] = "PAPER"

    # IMPORTANT: default to SIM so we don't accidentally trade
    # If you want to place an IB paper trade, you must ARM it in Terminal.
    if os.getenv("TRADE_LABS_EXECUTION_BACKEND") is None:
        os.environ["TRADE_LABS_EXECUTION_BACKEND"] = "SIM"

    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: SPY MVP (safe by default; uses 1-min bars for price)\n")
    print(f"EXECUTION_BACKEND={os.getenv('TRADE_LABS_EXECUTION_BACKEND')}  ARMED={os.getenv('TRADE_LABS_ARMED','0')}\n")

    intent = TradeIntent(
        symbol="SPY",
        side="BUY",
        entry_type="MKT",
        quantity=None,
        stop_loss=None,
        rationale="MVP: SPY using 1-min bars (avoids streaming perms)"
    )

    ib = connect_ib()
    contract = get_spy_contract()

    entry_price = get_recent_price_from_history(ib, contract)
    bars_df = get_history_bars(ib, contract, duration="30 D", bar_size="1 day")
    atr = compute_atr(bars_df, period=14)
    equity = get_account_equity_usd(ib)
    open_risk = estimate_open_risk_usd(ib, atr=atr, atr_multiplier=2.0)

    print(f"Recent Price (1-min bars): {entry_price}")
    print(f"ATR(14): {atr}")
    print(f"NetLiquidation: {equity}")
    print(f"Estimated Open Risk (USD): {open_risk}")

    result = execute_trade_intent_paper(
        intent=intent,
        ib=ib,
        account_equity_usd=equity,
        entry_price=entry_price,
        open_risk_usd=open_risk,
        atr=atr,
        atr_multiplier=2.0,
        risk_percent=0.005
    )

    ib.disconnect()

    print("\nPipeline Result:")
    print(result)


if __name__ == "__main__":
    main()
