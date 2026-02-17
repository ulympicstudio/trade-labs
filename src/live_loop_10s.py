import time
from datetime import datetime, timezone
from typing import Set, List, Dict, Tuple

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
# ---- Bracket Throttling & Safety ----
MAX_NEW_BRACKETS_PER_LOOP = 1           # Submit max 1 bracket per 10s loop
COOLDOWN_SECONDS_PER_SYMBOL = 300       # Don't retry same symbol for 5 min

# ---- Universe Filter (Stocks Only) ----
ALLOWED_SEC_TYPES = {"STK"}
ALLOWED_EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}
STOCK_ALLOWLIST = {"SPY", "QQQ"}                              # Always allow these
STOCK_BLOCKLIST = {"UNG", "SLV", "KOLD", "BITO"}             # Always block (commodity/leveraged ETFs)
ETF_KEYWORDS = {"ETF", "ETN", "FUND", "TRUST", "INDEX", "NOTE"}  # Reject if in longName

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


def is_valid_stock_contract(ib: IB, symbol: str) -> Tuple[bool, str]:
    """
    Validate that symbol is a tradeable stock (not ETF/ETN/etc).
    
    Returns:
        (is_valid, reason_if_invalid)
    """
    # Blocklist takes precedence
    if symbol in STOCK_BLOCKLIST:
        return False, f"In blocklist"
    
    # Allowlist always passes
    if symbol in STOCK_ALLOWLIST:
        return True, ""
    
    # Qualify and check secType
    try:
        c = _contract(symbol)
        ib.qualifyContracts(c)
        
        # Check secType
        if c.secType not in ALLOWED_SEC_TYPES:
            return False, f"secType={c.secType} not in {ALLOWED_SEC_TYPES}"
        
        # Check primaryExchange
        if c.primaryExchange and c.primaryExchange not in ALLOWED_EXCHANGES:
            return False, f"exchange={c.primaryExchange} not allowed"
        
        # Check longName for ETF keywords
        if hasattr(c, 'contractDetails'):
            long_name = (c.contractDetails.longName or "").upper()
            for kw in ETF_KEYWORDS:
                if kw in long_name:
                    return False, f"longName contains '{kw}'"
        
        return True, ""
    except Exception as e:
        return False, f"Qualification failed: {e}"


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
    
    # Track last bracket submission per symbol (for throttling)
    last_bracket_ts: Dict[str, float] = {}  # symbol -> timestamp of last bracket

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

                brackets_submitted_this_loop = 0  # Throttle: max 1 per loop
                
                for cand in scored:
                    sym = cand.symbol
                    
                    # Skip if already active
                    if sym in active:
                        continue
                    
                    # Skip if max positions reached
                    if len(active) >= MAX_CONCURRENT_POSITIONS:
                        print("Max concurrent positions reached.")
                        break
                    
                    # ====== FIX B: UNIVERSE FILTER (STOCKS ONLY) ======
                    is_valid, reason = is_valid_stock_contract(ib, sym)
                    if not is_valid:
                        print(f"[REJECT] {sym}: not tradeable ({reason})")
                        continue
                    
                    # ====== GET PRICE & CHECK MIN ======
                    try:
                        px = get_recent_price_1m(ib, sym)
                    except Exception as e:
                        print(f"[SKIP] {sym}: price fetch failed ({e})")
                        continue
                    
                    if px < MIN_PRICE:
                        continue

                    # ====== GET ATR & CHECK VALIDITY ======
                    atr = float(getattr(cand, "atr14", atr_cache.get(sym, 0.0)))
                    if atr <= 0:
                        continue

                    # ====== CALCULATE SIZING ======
                    stop_dist = atr * INITIAL_RISK_ATR_MULT
                    risk_dollars = equity * RISK_PER_TRADE
                    qty = int(risk_dollars // stop_dist)
                    if qty <= 0:
                        continue

                    # ====== FIX A: PRE-FLIGHT VALIDATION GATE ======
                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    tp = entry + (TAKE_PROFIT_R * stop_dist)
                    trail_amt = atr * TRAIL_ATR_MULT
                    
                    # Triple-check before submission
                    if entry is None or entry <= 0:
                        print(f"[VALIDATION] {sym}: entry price invalid (${entry})")
                        continue
                    
                    if atr <= 0:
                        print(f"[VALIDATION] {sym}: ATR invalid ({atr})")
                        continue
                    
                    if qty <= 0:
                        print(f"[VALIDATION] {sym}: qty invalid ({qty})")
                        continue

                    # ====== CHECK DAILY KILL SWITCH ======
                    if is_kill_switch_active(ib):
                        print(f"[KILL_SWITCH] Rejecting {sym}: daily loss threshold exceeded")
                        continue
                    
                    # ====== CHECK THROTTLE: COOLDOWN PER SYMBOL ======
                    last_bracket_time = last_bracket_ts.get(sym, 0.0)
                    if now - last_bracket_time < COOLDOWN_SECONDS_PER_SYMBOL:
                        cooldown_remain = COOLDOWN_SECONDS_PER_SYMBOL - (now - last_bracket_time)
                        print(f"[THROTTLE] {sym}: cooldown active ({cooldown_remain:.0f}s remaining)")
                        continue
                    
                    # ====== CHECK THROTTLE: MAX 1 PER LOOP ======
                    if brackets_submitted_this_loop >= MAX_NEW_BRACKETS_PER_LOOP:
                        print(f"[THROTTLE] {sym}: max {MAX_NEW_BRACKETS_PER_LOOP} bracket(s) per loop reached")
                        break
                    
                    # ====== SIM MODE ======
                    if not armed:
                        print(f"[SIM] {sym} qty={qty} entry={entry:.2f} tp={tp:.2f} trail={trail_amt:.2f}")
                        continue

                    # ====== SUBMIT BRACKET (ARMED MODE) ======
                    params = BracketParams(
                        symbol=sym, qty=qty,
                        entry_limit=entry, take_profit=tp, trail_amount=trail_amt,
                        tif="DAY"
                    )
                    res = place_limit_tp_trail_bracket(ib, params)
                    print(f"[IB] {sym} -> {res.ok} {res.message}")
                    
                    if res.ok:
                        active.add(sym)
                        last_bracket_ts[sym] = now
                        brackets_submitted_this_loop += 1

                last_symbols = current_symbols
                last_print_ts = now

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, LOOP_SECONDS - elapsed))

    except KeyboardInterrupt:
        print("\nStopping live loop.")
    finally:
        # Cancel pending async tasks before disconnect
        import asyncio
        try:
            pending = asyncio.all_tasks()
            for task in pending:
                task.cancel()
        except:
            pass
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
