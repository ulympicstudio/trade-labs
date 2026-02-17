import time
from datetime import datetime, timezone
from typing import Set, List, Dict

from ib_insync import IB, Stock, util

from config.identity import SYSTEM_NAME, HUMAN_NAME
from config.runtime import is_armed, execution_backend, is_paper
from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID

from src.execution.bracket_orders import BracketParams, place_limit_tp_trail_bracket
from src.signals.market_scanner import scan_us_most_active_stocks
from src.signals.score_candidates import score_scan_results
from src.risk.daily_pnl_manager import (
    record_session_start_equity, is_kill_switch_active, get_kill_switch_status
)


# ---- Risk Framework ----
RISK_PER_TRADE = 0.005
MAX_TOTAL_OPEN_RISK = 0.025
MAX_CONCURRENT_POSITIONS = 6
DAILY_KILL_SWITCH = 0.015  # placeholder (we’ll wire true PnL next)

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

# Cache ATR so we don’t request daily bars repeatedly
ATR_CACHE_SECONDS = 600  # 10 minutes


def connect_ib() -> IB:
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)

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
        c, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
        whatToShow="TRADES", useRTH=False, formatDate=1
    )
    df = util.df(bars)
    return float(df["close"].iloc[-1])


def get_daily_30d(ib: IB, symbol: str):
    c = _contract(symbol)
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(
        c, endDateTime="", durationStr="30 D", barSizeSetting="1 day",
        whatToShow="TRADES", useRTH=True, formatDate=1
    )
    return util.df(bars)


def atr14_from_daily(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = tr1.combine(tr2, max).combine(tr3, max)

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


def compute_open_risk_pct(ib: IB, equity: float, atr_cache: Dict[str, float]) -> float:
    """
    Open risk % = sum(shares * ATR * 2) / equity
    Uses ATR cache; if ATR missing, counts 0 risk for that symbol (conservative).
    """
    if equity <= 0:
        return 0.0

    total_risk_usd = 0.0
    for p in ib.positions():
        if p.position == 0:
            continue
        sym = p.contract.symbol
        shares = abs(float(p.position))
        atr = atr_cache.get(sym, 0.0)
        total_risk_usd += shares * (atr * INITIAL_RISK_ATR_MULT)

    return total_risk_usd / equity


def main():
    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Live Loop (10s)")
    print(f"MODE={'PAPER' if is_paper() else 'LIVE'} BACKEND={execution_backend()} ARMED={is_armed()}\n")

    ib = connect_ib()

    cached_scan = []
    last_scan_ts = 0.0
    last_print_ts = 0.0
    last_symbols: List[str] = []
    
    # Session tracking for daily kill switch
    session_started = False
    last_session_date = None

    atr_cache: Dict[str, float] = {}
    atr_cache_ts: Dict[str, float] = {}

    try:
        while True:
            loop_start = time.time()
            now = time.time()
            armed = is_armed()

            equity = get_equity(ib)
            
            # Record session start on first run or new trading day
            current_session_date = datetime.now(timezone.utc).date()
            if not session_started or last_session_date != current_session_date:
                record_session_start_equity(equity)
                session_started = True
                last_session_date = current_session_date
                print(f"[SESSION] Started with equity: ${equity:,.2f}")

            # Refresh scanner periodically
            if (now - last_scan_ts) >= SCAN_REFRESH_SECONDS or not cached_scan:
                try:
                    cached_scan = scan_us_most_active_stocks(ib, limit=SCAN_LIMIT)
                    last_scan_ts = now
                    print(f"[SCAN] refreshed: {len(cached_scan)}")
                except Exception as e:
                    print(f"[SCAN] error: {e}")

            scan_for_scoring = cached_scan[:SCORE_TOP_N_FROM_SCAN]
            scored = score_scan_results(ib, scan_for_scoring, top_n=TRADE_TOP_N)

            active = get_active_symbols(ib)
            current_symbols = [c.symbol for c in scored]

            # Refresh ATR cache for scored symbols (every 10 min per symbol)
            for cnd in scored:
                sym = cnd.symbol
                last_t = atr_cache_ts.get(sym, 0.0)
                if (now - last_t) >= ATR_CACHE_SECONDS or sym not in atr_cache:
                    try:
                        df_d = get_daily_30d(ib, sym)
                        atr_cache[sym] = atr14_from_daily(df_d)
                        atr_cache_ts[sym] = now
                    except Exception:
                        atr_cache[sym] = atr_cache.get(sym, 0.0)

            open_risk_pct = compute_open_risk_pct(ib, equity, atr_cache)

            if open_risk_pct >= MAX_TOTAL_OPEN_RISK:
                if (now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS:
                    print(f"Max open risk reached: {open_risk_pct:.3f} >= {MAX_TOTAL_OPEN_RISK:.3f}. No new trades.")
                    last_print_ts = now
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            should_print = (current_symbols != last_symbols) or ((now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS)

            if should_print:
                print(f"\n--- Loop --- ARMED={armed} equity={equity:,.0f} open_risk={open_risk_pct:.3f} active={len(active)}")

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

                    atr = float(getattr(cand, "atr14", atr_cache.get(sym, 0.0)))
                    if atr <= 0:
                        continue

                    stop_dist = atr * INITIAL_RISK_ATR_MULT
                    risk_dollars = equity * RISK_PER_TRADE
                    qty = int(risk_dollars // stop_dist)
                    if qty <= 0:
                        continue

                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    tp = entry + (TAKE_PROFIT_R * stop_dist)
                    trail_amt = atr * TRAIL_ATR_MULT

                    # Check daily kill switch before placing any trade
                    if is_kill_switch_active(ib):
                        print(f"[KILL_SWITCH] Rejecting {sym}: daily loss threshold exceeded")
                        continue

                    if not armed:
                        print(f"[SIM] {sym} qty={qty} entry={entry:.2f} tp={tp:.2f} trail={trail_amt:.2f}")
                        continue

                    params = BracketParams(
                        symbol=sym, qty=qty,
                        entry_limit=entry, take_profit=tp, trail_amount=trail_amt,
                        tif="DAY"
                    )
                    res = place_limit_tp_trail_bracket(ib, params)
                    print(f"[IB] {sym} -> {res.ok} {res.message}")
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
