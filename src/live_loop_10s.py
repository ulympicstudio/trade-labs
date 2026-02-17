import time
from typing import Set, List

from ib_insync import IB, Stock, util

from config.identity import SYSTEM_NAME, HUMAN_NAME
from config.runtime import is_armed, execution_backend, is_paper
from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID

from src.execution.bracket_orders import BracketParams, place_limit_tp_trail_bracket
from src.signals.market_scanner import scan_us_most_active_stocks
from src.signals.score_candidates import score_scan_results
from src.utils.market_hours import is_market_open


# ---- Risk Framework ----
RISK_PER_TRADE = 0.005          # 0.5%
MAX_TOTAL_OPEN_RISK = 0.025     # 2.5%
MAX_CONCURRENT_POSITIONS = 6
DAILY_KILL_SWITCH = 0.015       # -1.5%

# ---- Loop Settings ----
LOOP_SECONDS = 10
SCAN_REFRESH_SECONDS = 300
SCAN_LIMIT = 30
SCORE_TOP_N_FROM_SCAN = 20
TRADE_TOP_N = 6

ENTRY_OFFSET_PCT = 0.0005
TAKE_PROFIT_R = 1.5
INITIAL_RISK_ATR_MULT = 2.0
TRAIL_ATR_MULT = 1.2

MIN_PRICE = 5.0
PRINT_HEARTBEAT_SECONDS = 60


def connect_ib() -> IB:
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)

    # Suppress informational IB noise
    orig_error = ib.wrapper.error

    def quiet_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (162, 10089):
            return
        return orig_error(reqId, errorCode, errorString, advancedOrderRejectJson)

    ib.wrapper.error = quiet_error
    return ib


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def get_recent_price_1m(ib: IB, symbol: str) -> float:
    c = _contract(symbol)
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=1
    )
    df = util.df(bars)
    return float(df["close"].iloc[-1])


def get_daily_30d(ib: IB, symbol: str):
    c = _contract(symbol)
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="30 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1
    )
    return util.df(bars)


def atr14_from_daily(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = (high - low).combine((high - prev_close).abs(), max)
    atr = tr.rolling(14).mean().dropna()
    return float(atr.iloc[-1])


def get_equity(ib: IB) -> float:
    for v in ib.accountValues():
        if v.tag == "NetLiquidation":
            return float(v.value)
    return 0.0


def get_active_symbols(ib: IB) -> Set[str]:
    syms = set()
    for tr in ib.openTrades():
        syms.add(tr.contract.symbol)
    for p in ib.positions():
        if p.position != 0:
            syms.add(p.contract.symbol)
    return syms


def compute_open_risk(ib: IB) -> float:
    # simple approximation: assume each open trade risks RISK_PER_TRADE
    open_positions = len(ib.positions())
    return open_positions * RISK_PER_TRADE


def main():
    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Live Loop (10s)")
    print(f"MODE={'PAPER' if is_paper() else 'LIVE'} BACKEND={execution_backend()} ARMED={is_armed()}\n")

    ib = connect_ib()

    cached_scan = []
    last_scan_ts = 0.0
    last_print_ts = 0.0
    last_symbols: List[str] = []

    try:
        while True:
            loop_start = time.time()
            now = time.time()
            armed = is_armed()

            equity = get_equity(ib)
            open_risk = compute_open_risk(ib)

            # Kill switch
            if open_risk >= MAX_TOTAL_OPEN_RISK:
                print("Max total open risk reached. No new trades.")
                time.sleep(LOOP_SECONDS)
                continue

            # Scanner refresh (only during market hours)
            if is_market_open():
                if (now - last_scan_ts) >= SCAN_REFRESH_SECONDS or not cached_scan:
                    try:
                        cached_scan = scan_us_most_active_stocks(ib, limit=SCAN_LIMIT)
                        last_scan_ts = now
                        print(f"[SCAN] refreshed: {len(cached_scan)}")
                    except Exception as e:
                        print(f"[SCAN] error: {e}")
            else:
                print("[SCAN] market closed, skipping refresh")

            scan_for_scoring = cached_scan[:SCORE_TOP_N_FROM_SCAN]
            scored = score_scan_results(ib, scan_for_scoring, top_n=TRADE_TOP_N)

            active = get_active_symbols(ib)
            current_symbols = [c.symbol for c in scored]

            if current_symbols != last_symbols or (now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS:
                print(f"\n--- Loop --- ARMED={armed} equity={equity:,.0f} open_risk={open_risk:.3f}")

                for cand in scored:
                    sym = cand.symbol

                    if sym in active:
                        continue

                    if len(active) >= MAX_CONCURRENT_POSITIONS:
                        print("Max concurrent positions reached.")
                        break

                    px = get_recent_price_1m(ib, sym)
                    if px < MIN_PRICE:
                        continue

                    df_d = get_daily_30d(ib, sym)
                    atr = atr14_from_daily(df_d)

                    stop_dist = atr * INITIAL_RISK_ATR_MULT
                    risk_dollars = equity * RISK_PER_TRADE
                    qty = int(risk_dollars // stop_dist)

                    if qty <= 0:
                        continue

                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    tp = entry + (TAKE_PROFIT_R * stop_dist)
                    trail_amt = atr * TRAIL_ATR_MULT

                    if not armed:
                        print(f"[SIM] {sym} qty={qty} entry={entry:.2f} tp={tp:.2f} trail={trail_amt:.2f}")
                        continue

                    params = BracketParams(
                        symbol=sym,
                        qty=qty,
                        entry_limit=entry,
                        take_profit=tp,
                        trail_amount=trail_amt
                    )

                    res = place_limit_tp_trail_bracket(ib, params)
                    print(f"[IB] {sym} -> {res.ok}")

                    active.add(sym)

                last_symbols = current_symbols
                last_print_ts = now

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, LOOP_SECONDS - elapsed))

    except KeyboardInterrupt:
        print("\nStopping live loop.")
    finally:
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
