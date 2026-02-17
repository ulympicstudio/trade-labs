import time
import os
import json
import math
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Set, List, Dict, Tuple, Optional

import requests
from ib_insync import IB, Stock, util, MarketOrder

from config.identity import SYSTEM_NAME, HUMAN_NAME
from config.runtime import is_armed, execution_backend, is_paper
from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID
from config.universe_filter import ALLOWED_SEC_TYPES, ALLOWED_EXCHANGES, STOCK_ALLOWLIST, STOCK_BLOCKLIST, ETF_KEYWORDS

from src.execution.bracket_orders import (
    BracketParams,
    place_limit_tp_trail_bracket,
    place_trailing_stop,
)
from src.signals.market_scanner import scan_us_most_active_stocks
from src.signals.score_candidates import score_scan_results
from src.risk.daily_pnl_manager import (
    record_session_start_equity, is_kill_switch_active, get_kill_switch_status
)

# ====== CATALYST-DRIVEN TRADING ENGINE ======
try:
    from src.data.catalyst_hunter import CatalystHunter
    from src.data.catalyst_scorer import CatalystScorer
    from src.data.research_engine import ResearchEngine
    CATALYST_ENGINE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Catalyst engine not available: {e}")
    CATALYST_ENGINE_AVAILABLE = False


# ---- Risk Framework ----
BASE_RISK_PER_TRADE = 0.005
CONVICTION_RISK_PER_TRADE = 0.0075
BASE_MAX_TOTAL_OPEN_RISK = 0.02
CONVICTION_MAX_TOTAL_OPEN_RISK = 0.045
BASE_MAX_CONCURRENT_POSITIONS = 4
CONVICTION_MAX_CONCURRENT_POSITIONS = 6
MIN_CATALYST_SCORE = 65.0  # Catalyst score threshold for trading (lowered to allow quality trending signals)

LOSS_STREAK_LOOKBACK = 2
LOSS_STREAK_PENALTY_TRADES = 3
LOSS_STREAK_RISK_REDUCTION = 0.75

PEAK_DRAWDOWN_THRESHOLD = 0.05
PEAK_DRAWDOWN_RISK_PCT = 0.004
# ---- Loop Settings ----
LOOP_SECONDS = 10
SCAN_REFRESH_SECONDS = 300
SCAN_LIMIT = 30
SCORE_TOP_N_FROM_SCAN = 20
TRADE_TOP_N = 12  # Top N scored candidates to evaluate (was 6, show more variety)
MIN_CATALYSTS_FOR_SCAN = 3

ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.5  # How many ATRs below entry is our hard stop
TRAIL_ATR_MULT = 1.2
TRAIL_ACTIVATE_ATR = 1.5
TRAIL_CHECK_SECONDS = 30

MIN_PRICE = 5.0
PRINT_HEARTBEAT_SECONDS = 60

# Cache ATR so we don’t request daily bars repeatedly
ATR_CACHE_SECONDS = 600  # 10 minutes
# Market breadth cache
BREADTH_CACHE_SECONDS = 300
BREADTH_ADV_THRESHOLD = 0.65
BREADTH_SCAN_LIMIT = 15
# Trade history for loss-streak tracking
TRADES_FILE = Path("data/trade_history/trades.json")
# ---- Bracket Throttling & Safety ----
MAX_NEW_BRACKETS_PER_LOOP = 1           # Submit max 1 bracket per 10s loop
COOLDOWN_SECONDS_PER_SYMBOL = 300       # Don't retry same symbol for 5 min
BRACKET_COOLDOWN_SECONDS = int(os.getenv("TRADE_LABS_BRACKET_COOLDOWN_SECONDS", "60"))

# Session-level invalid symbol cache (to suppress repeated IB errors)
INVALID_SYMBOL_CACHE: Set[str] = set()

# Delayed trailing state tracking
TRAIL_STATE: Dict[str, str] = {}  # symbol -> "pending" | "activated"
TRAIL_LOGGED: Set[str] = set()  # session-level dedup for activation logs


def connect_ib() -> IB:
    ib = IB()
    ib.RequestTimeout = 5
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)

    orig_error = ib.wrapper.error
    def quiet_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (162, 10089, 10168):
            return
        if errorCode == 200 and "No security definition" in errorString:
            return
        return orig_error(reqId, errorCode, errorString, advancedOrderRejectJson)
    ib.wrapper.error = quiet_error

    return ib


def is_valid_stock_contract(
    ib: IB,
    symbol: str,
    valid_contracts: Optional[Dict[str, Stock]] = None,
) -> Tuple[bool, str]:
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
        if valid_contracts is not None and symbol in valid_contracts:
            c = valid_contracts[symbol]
        else:
            c = _contract(symbol)
            ib.qualifyContracts(c)
        
        # Check secType (MUST be STK)
        if c.secType != "STK":
            return False, f"secType={c.secType} (not STK)"
        
        # Check primaryExchange
        if c.primaryExchange and c.primaryExchange not in ALLOWED_EXCHANGES:
            return False, f"exchange={c.primaryExchange} not allowed"
        
        # Fetch full contract details to check longName
        try:
            contract_details = ib.reqContractDetails(c)
            if contract_details and len(contract_details) > 0:
                long_name = (contract_details[0].longName or "").upper()
                print(f"    [CHECK] {symbol} secType={c.secType} longName={long_name}")
                for kw in ETF_KEYWORDS:
                    # Use word boundary check to avoid false positives like "NETFLIX" containing "ETF"
                    if re.search(rf"\b{re.escape(kw)}\b", long_name):
                        return False, f"longName contains '{kw}'"
        except Exception as e:
            print(f"    [WARN] Could not fetch details for {symbol}: {e}")
        
        return True, ""
    except Exception as e:
        return False, f"Qualification failed: {e}"


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def get_recent_price_1m(ib: IB, symbol: str) -> float:
    c = _contract(symbol)
    ib.qualifyContracts(c)
    try:
        bars = ib.reqHistoricalData(
            c, endDateTime="", durationStr="1 D", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=False, formatDate=1
        )
        df = util.df(bars)
        if df is not None and not df.empty and "close" in df.columns:
            px = float(df["close"].iloc[-1])
            if math.isfinite(px) and px > 0:
                return px
    except Exception:
        pass

    try:
        df_d = get_daily_30d(ib, symbol)
        if df_d is not None and not df_d.empty and "close" in df_d.columns:
            px = float(df_d["close"].iloc[-1])
            if math.isfinite(px) and px > 0:
                return px
    except Exception:
        pass

    snap = get_last_price_snapshot(ib, c)
    if snap is not None:
        return float(snap)
    raise RuntimeError("no price data")


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
    Open risk % = sum(shares * ATR * STOP_LOSS_R) / equity
    
    Includes:
    - Filled positions (ib.positions)
    - Pending limit orders (openTrades) - counts as "reserved risk"
    
    This prevents over-stacking orders before fills in autonomous mode.
    Uses ATR cache; if ATR missing, counts 0 risk for that symbol (conservative).
    """
    if equity <= 0:
        return 0.0

    total_risk_usd = 0.0
    
    # Count filled positions
    for p in ib.positions():
        if p.position == 0:
            continue
        sym = p.contract.symbol
        shares = abs(float(p.position))
        atr = atr_cache.get(sym, 0.0)
        total_risk_usd += shares * (atr * STOP_LOSS_R)
    
    # Count pending limit orders (reserved risk)
    filled_symbols = {p.contract.symbol for p in ib.positions() if p.position != 0}
    for trade in ib.openTrades():
        # Only count parent BUY/SELL limit orders (not stops/trails)
        if trade.order.orderType != "LMT":
            continue
        if trade.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
            continue
        
        sym = trade.contract.symbol
        # Skip if already counted as filled position
        if sym in filled_symbols:
            continue
        
        shares = abs(float(trade.order.totalQuantity))
        atr = atr_cache.get(sym, 0.0)
        total_risk_usd += shares * (atr * STOP_LOSS_R)

    return total_risk_usd / equity


def get_market_breadth_pct() -> Optional[float]:
    """
    Approximate market breadth using Yahoo Finance day gainers/losers counts.
    Returns a ratio in [0, 1] or None on failure.
    """
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"

    def fetch_total(scr_id: str) -> Optional[int]:
        try:
            resp = requests.get(url, params={"scrIds": scr_id, "count": 0, "start": 0}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("finance", {}).get("result", [])
            if not result:
                return None
            return int(result[0].get("total") or 0)
        except Exception:
            return None

    gainers = fetch_total("day_gainers")
    losers = fetch_total("day_losers")
    if gainers is None or losers is None:
        return None
    total = gainers + losers
    if total <= 0:
        return None
    return gainers / total


def get_scan_breadth_pct(ib: IB, scan_results: List) -> Optional[float]:
    if not scan_results:
        return None

    adv = 0
    dec = 0

    for r in scan_results[:BREADTH_SCAN_LIMIT]:
        sym = getattr(r, "symbol", None)
        if not sym:
            continue
        try:
            df_d = get_daily_30d(ib, sym)
            if df_d is None or df_d.empty or len(df_d) < 2:
                continue
            last_close = float(df_d["close"].iloc[-1])
            prev_close = float(df_d["close"].iloc[-2])
            if last_close > prev_close:
                adv += 1
            elif last_close < prev_close:
                dec += 1
        except Exception:
            continue

    total = adv + dec
    if total <= 0:
        return None
    return adv / total


def load_closed_trades() -> List[Dict[str, float]]:
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE) as f:
            trades = json.load(f)
        return [t for t in trades if t.get("status") == "CLOSED" and t.get("pnl") is not None]
    except Exception:
        return []


def cancel_order_by_id(ib: IB, order_id: int) -> bool:
    if not order_id:
        return False
    for trade in ib.openTrades():
        if trade.order and trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            return True
    return False


def get_last_price_snapshot(ib: IB, contract: Stock) -> Optional[float]:
    try:
        market_data = ib.reqMktData(contract, "", True, False)
        ib.sleep(0.1)
        if market_data.last is not None and market_data.last > 0:
            return float(market_data.last)
        if market_data.bid is not None and market_data.ask is not None:
            return float((market_data.bid + market_data.ask) / 2.0)
    except Exception:
        return None
    return None


def close_positions_by_weakness(ib: IB, armed: bool) -> None:
    positions = [p for p in ib.portfolio() if p.position != 0]
    positions.sort(key=lambda p: float(p.unrealizedPNL or 0.0))

    for p in positions:
        action = "SELL" if p.position > 0 else "BUY"
        qty = abs(int(p.position))
        if qty <= 0:
            continue
        if not armed:
            print(f"[KILL_SWITCH] SIM close {p.contract.symbol} qty={qty}")
            continue
        order = MarketOrder(action, qty)
        ib.placeOrder(p.contract, order)
        ib.sleep(0.1)


def main():
    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Live Loop (10s)")
    print(f"MODE={'PAPER' if is_paper() else 'LIVE'} BACKEND={execution_backend()} ARMED={is_armed()}\n")
    print(f"[COOLDOWN] BRACKET_COOLDOWN_SECONDS={BRACKET_COOLDOWN_SECONDS}")

    ib = connect_ib()

    # ====== INITIALIZE CATALYST ENGINE (PRIMARY) ======
    research_engine = None
    if CATALYST_ENGINE_AVAILABLE:
        try:
            finnhub_key = os.getenv("FINNHUB_API_KEY")
            hunter = CatalystHunter(finnhub_api_key=finnhub_key)
            scorer = CatalystScorer()
            research_engine = ResearchEngine(
                catalyst_hunter=hunter,
                catalyst_scorer=scorer,
            )
            print("✅ [CATALYST ENGINE] Initialized (PRIMARY source)")
        except Exception as e:
            print(f"⚠️  [CATALYST ENGINE] Failed to init: {e}")
            research_engine = None

    cached_scan = []
    last_scan_ts = 0.0
    last_catalyst_hunt_ts = 0.0
    last_print_ts = 0.0
    last_symbols: List[str] = []
    
    # Session tracking for daily kill switch
    session_started = False
    last_session_date = None

    atr_cache: Dict[str, float] = {}
    atr_cache_ts: Dict[str, float] = {}
    
    # Catalyst trading candidates cache
    catalyst_candidates = []
    catalyst_ranking = []

    # Invalid symbol cache (session-level)
    invalid_symbols: Set[str] = INVALID_SYMBOL_CACHE
    invalid_symbols_logged: Set[str] = set()

    # Valid contract cache (session-level)
    valid_contracts: Dict[str, Stock] = {}
    
    # Track last bracket submission per symbol (for throttling)
    last_bracket_ts: Dict[str, float] = {}  # symbol -> timestamp of last bracket
    last_bracket_attempt_ts = 0.0
    stop_order_ids: Dict[str, int] = {}
    entry_atr_by_symbol: Dict[str, float] = {}
    trail_active_symbols: Set[str] = set()

    last_trail_check_ts = 0.0
    last_breadth_ts = 0.0
    last_breadth_pct: Optional[float] = None
    last_breadth_source = "n/a"

    last_scan_score_ts = 0.0
    last_scanner_scored = []

    last_closed_trade_count = 0
    loss_streak_penalty_remaining = 0
    session_peak_equity = 0.0

    trading_halted_for_day = False
    halted_date = None

    try:
        while True:
            loop_start = time.time()
            now = time.time()
            armed = is_armed()

            if not ib.isConnected():
                print("[WARN] IB disconnected, reconnecting...")
                try:
                    ib.disconnect()
                except Exception:
                    pass
                ib = connect_ib()
                time.sleep(1.0)
                continue

            equity = get_equity(ib)
            if equity <= 0:
                print("[WARN] Equity unavailable; retrying next loop.")
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue
            
            # Record session start on first run or new trading day
            current_session_date = datetime.now(timezone.utc).date()
            if not session_started or last_session_date != current_session_date:
                record_session_start_equity(equity)
                session_started = True
                last_session_date = current_session_date
                print(f"[SESSION] Started with equity: ${equity:,.2f}")

            if session_peak_equity <= 0:
                session_peak_equity = equity
            else:
                session_peak_equity = max(session_peak_equity, equity)

            if trading_halted_for_day and halted_date != current_session_date:
                trading_halted_for_day = False
                halted_date = None

            if is_kill_switch_active(ib):
                if not trading_halted_for_day:
                    print("[KILL_SWITCH] Triggered. Canceling orders and flattening positions.")
                    try:
                        ib.reqGlobalCancel()
                    except Exception:
                        pass
                    close_positions_by_weakness(ib, armed)
                    trading_halted_for_day = True
                    halted_date = current_session_date
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            if trading_halted_for_day:
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            closed_trades = load_closed_trades()
            if len(closed_trades) > last_closed_trade_count:
                if len(closed_trades) >= LOSS_STREAK_LOOKBACK:
                    last_two = closed_trades[-LOSS_STREAK_LOOKBACK:]
                    if all((t.get("pnl") or 0.0) < 0 for t in last_two):
                        loss_streak_penalty_remaining = LOSS_STREAK_PENALTY_TRADES
                last_closed_trade_count = len(closed_trades)

            # ====== CATALYST HUNTING (PRIMARY - every 5 minutes) ======
            catalyst_hunt_interval = 300  # Hunt catalysts every 5 minutes
            catalyst_refreshed = False
            if research_engine and ((now - last_catalyst_hunt_ts) >= catalyst_hunt_interval or not catalyst_candidates):
                try:
                    catalyst_hunt_results = research_engine.hunt_all_sources()
                    catalyst_ranking = research_engine.scorer.rank_opportunities(catalyst_hunt_results, max_results=20)
                    catalyst_candidates = [opp.symbol for opp in catalyst_ranking[:10]]
                    last_catalyst_hunt_ts = now
                    catalyst_refreshed = True
                    print(f"[CATALYST] Found {len(catalyst_candidates)} high-quality opportunities")
                except Exception as e:
                    print(f"[CATALYST] hunt error: {e}")

            # ====== SCANNER HUNTING (SECONDARY - fallback) ======
            if (now - last_scan_ts) >= SCAN_REFRESH_SECONDS or not cached_scan:
                try:
                    cached_scan = scan_us_most_active_stocks(ib, limit=SCAN_LIMIT)
                    last_scan_ts = now
                    print(f"[SCAN] refreshed: {len(cached_scan)} (fallback/validation)")
                except Exception as e:
                    print(f"[SCAN] error: {e}")

            # ====== BLEND SOURCES: CATALYST PRIMARY + SCANNER FALLBACK ======
            # Priority: 1) Catalyst candidates, 2) Scanner results
            scored = []
            
            # First: use catalyst ranking directly (already scored by catalyst scorer)
            if catalyst_ranking:
                # catalyst_ranking is already a list of CatalystScore objects sorted by score
                # Validate contracts with IB before scoring
                catalyst_contracts = []
                
                for opp in catalyst_ranking[:TRADE_TOP_N]:
                    if opp.symbol in invalid_symbols:
                        continue
                    if opp.symbol in valid_contracts:
                        c = valid_contracts[opp.symbol]
                        c.catalyst_score = opp.combined_score
                        catalyst_contracts.append(c)
                        continue
                    try:
                        c = Stock(opp.symbol, "SMART", "USD")
                        # Try to qualify - this validates the symbol exists with IB
                        qualified = ib.qualifyContracts(c)
                        
                        if qualified:
                            # Valid contract - use it
                            c.catalyst_score = opp.combined_score
                            catalyst_contracts.append(c)
                            valid_contracts[opp.symbol] = c
                        else:
                            # Failed validation
                            invalid_symbols.add(opp.symbol)
                            if opp.symbol not in invalid_symbols_logged:
                                print(f"  [CATALYST REJECTED] {opp.symbol}: invalid contract")
                                invalid_symbols_logged.add(opp.symbol)
                    except Exception as e:
                        # Contract lookup failed
                        invalid_symbols.add(opp.symbol)
                        if opp.symbol not in invalid_symbols_logged:
                            reason = str(e).strip() or "qualification failed"
                            print(f"  [CATALYST REJECTED] {opp.symbol}: {reason}")
                            invalid_symbols_logged.add(opp.symbol)
                
                scored.extend(catalyst_contracts)
                if catalyst_refreshed:
                    print(f"  [CATALYST SCORED] {len(catalyst_contracts)} candidates ready (catalyst score source)")
            
            # Fallback: only supplement if catalysts are scarce
            if len(scored) < MIN_CATALYSTS_FOR_SCAN:
                if (now - last_scan_score_ts) >= SCAN_REFRESH_SECONDS or not last_scanner_scored:
                    scan_for_scoring = cached_scan[:SCORE_TOP_N_FROM_SCAN]
                    last_scanner_scored = score_scan_results(ib, scan_for_scoring, top_n=TRADE_TOP_N)
                    last_scan_score_ts = now

                scanner_scored = last_scanner_scored
                
                # Dedup: don't include scanner results already in catalyst
                catalyst_syms = set(s.symbol for s in scored)
                scanner_only = [s for s in scanner_scored if s.symbol not in catalyst_syms]
                
                scored.extend(scanner_only[:max(0, TRADE_TOP_N - len(scored))])
                
                if scanner_only:
                    print(f"  [SCANNER] Added {len(scanner_only[:max(0, TRADE_TOP_N - len(scored))])} fallback candidates")

            active = get_active_symbols(ib)
            current_symbols = [c.symbol for c in scored]

            if (now - last_breadth_ts) >= BREADTH_CACHE_SECONDS or last_breadth_pct is None:
                last_breadth_pct = get_scan_breadth_pct(ib, cached_scan)
                last_breadth_source = "scan"
                if last_breadth_pct is None:
                    last_breadth_pct = get_market_breadth_pct()
                    last_breadth_source = "yahoo" if last_breadth_pct is not None else "n/a"
                last_breadth_ts = now

            high_score_count = 0
            earnings_high_conf_count = 0
            if catalyst_ranking:
                high_score_count = sum(1 for opp in catalyst_ranking if opp.combined_score >= 75)
                earnings_high_conf_count = sum(
                    1
                    for opp in catalyst_ranking
                    if "earnings" in opp.best_catalyst_types and opp.confidence >= 0.9
                )

            breadth_trigger = (last_breadth_pct is not None) and (last_breadth_pct >= BREADTH_ADV_THRESHOLD)
            conviction_mode = (
                high_score_count >= 3
                or earnings_high_conf_count >= 2
                or breadth_trigger
            )

            # Build conviction reasons for audit trail
            conviction_reasons = []
            if high_score_count >= 3:
                conviction_reasons.append(f"high_scores={high_score_count}>=3")
            if earnings_high_conf_count >= 2:
                conviction_reasons.append(f"earnings_conf={earnings_high_conf_count}>=2")
            if breadth_trigger:
                conviction_reasons.append(f"breadth={last_breadth_pct*100:.1f}%>=65%")

            risk_per_trade_pct = CONVICTION_RISK_PER_TRADE if conviction_mode else BASE_RISK_PER_TRADE
            max_total_open_risk = CONVICTION_MAX_TOTAL_OPEN_RISK if conviction_mode else BASE_MAX_TOTAL_OPEN_RISK
            max_concurrent_positions = (
                CONVICTION_MAX_CONCURRENT_POSITIONS if conviction_mode else BASE_MAX_CONCURRENT_POSITIONS
            )

            drawdown_pct = 0.0
            if session_peak_equity > 0:
                drawdown_pct = max(0.0, (session_peak_equity - equity) / session_peak_equity)
                if drawdown_pct >= PEAK_DRAWDOWN_THRESHOLD:
                    risk_per_trade_pct = min(risk_per_trade_pct, PEAK_DRAWDOWN_RISK_PCT)

            if loss_streak_penalty_remaining > 0:
                risk_per_trade_pct *= LOSS_STREAK_RISK_REDUCTION

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

            if (now - last_trail_check_ts) >= TRAIL_CHECK_SECONDS:
                last_trail_check_ts = now
                for p in ib.positions():
                    if p.position <= 0:
                        continue
                    sym = p.contract.symbol
                    
                    # Skip if already activated
                    if sym in trail_active_symbols:
                        continue

                    # Get or compute ATR
                    atr = atr_cache.get(sym, 0.0)
                    if atr <= 0:
                        try:
                            df_d = get_daily_30d(ib, sym)
                            atr = atr14_from_daily(df_d)
                            atr_cache[sym] = atr
                        except Exception:
                            continue

                    entry_price = float(getattr(p, "avgCost", 0.0) or 0.0)
                    if entry_price <= 0:
                        continue

                    # Use 1m bars for price (not snapshot) to avoid NaN
                    try:
                        current_px = get_recent_price_1m(ib, sym)
                    except Exception:
                        continue

                    activation_px = entry_price + (TRAIL_ACTIVATE_ATR * atr)
                    
                    # Check if trail should be activated
                    if current_px >= activation_px:
                        # Activate trail
                        trail_amt = atr * TRAIL_ATR_MULT
                        stop_id = stop_order_ids.get(sym)
                        if stop_id:
                            cancel_order_by_id(ib, stop_id)

                        qty = abs(int(p.position))
                        if not armed:
                            print(f"[SIM] Activate trail {sym} qty={qty} trail=${trail_amt:.2f}")
                            trail_active_symbols.add(sym)
                            TRAIL_STATE[sym] = "activated"
                            continue

                        res = place_trailing_stop(ib, sym, qty, trail_amt, tif="DAY")
                        if res.ok:
                            trail_active_symbols.add(sym)
                            TRAIL_STATE[sym] = "activated"
                            if sym not in TRAIL_LOGGED:
                                print(f"[TRAIL_ACTIVATED] {sym} (order_id={res.trail_id}) trail=${trail_amt:.2f}")
                                TRAIL_LOGGED.add(sym)
                    else:
                        # Trail not yet activated - log PENDING once
                        if TRAIL_STATE.get(sym) != "pending":
                            TRAIL_STATE[sym] = "pending"
                            profit_pct = ((current_px - entry_price) / entry_price) * 100
                            print(
                                f"[TRAIL_PENDING] {sym} price=${current_px:.2f} entry=${entry_price:.2f} "
                                f"(+{profit_pct:.1f}%) activate_at=${activation_px:.2f}"
                            )

            open_risk_pct = compute_open_risk_pct(ib, equity, atr_cache)

            if open_risk_pct >= max_total_open_risk:
                if (now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS:
                    print(f"Max open risk reached: {open_risk_pct:.3f} >= {max_total_open_risk:.3f}. No new trades.")
                    last_print_ts = now
                time.sleep(max(0.0, LOOP_SECONDS - (time.time() - loop_start)))
                continue

            should_print = (current_symbols != last_symbols) or ((now - last_print_ts) >= PRINT_HEARTBEAT_SECONDS)

            if should_print:
                engine_status = "🎯 CATALYST PRIMARY" if research_engine else "📊 SCANNER"
                if last_breadth_pct is not None:
                    breadth_pct_display = f"{last_breadth_pct*100:.1f}% ({last_breadth_source})"
                else:
                    breadth_pct_display = "n/a"
                mode_label = "CONVICTION" if conviction_mode else "BASE"
                conviction_audit = f" [{', '.join(conviction_reasons)}]" if conviction_mode else ""
                print(
                    f"\n--- Loop --- ARMED={armed} mode={mode_label}{conviction_audit} equity={equity:,.0f} "
                    f"open_risk={open_risk_pct:.3f} active={len(active)} breadth={breadth_pct_display} {engine_status}"
                )

                brackets_submitted_this_loop = 0  # Throttle: max 1 per loop
                
                for cand in scored:
                    sym = cand.symbol

                    if sym in invalid_symbols:
                        continue
                    
                    # Skip if already active
                    if sym in active:
                        continue
                    
                    # Skip if max positions reached
                    if len(active) >= max_concurrent_positions:
                        print("Max concurrent positions reached.")
                        break
                    
                    # ====== SCORE THRESHOLD CHECK (CATALYST ONLY) ======
                    # Skip candidates from catalyst engine if below MIN_CATALYST_SCORE
                    if hasattr(cand, 'catalyst_score') and cand.catalyst_score is not None:
                        if cand.catalyst_score < MIN_CATALYST_SCORE:
                            continue  # Skip silently - score too low
                    
                    # ====== FIX B: UNIVERSE FILTER (STOCKS ONLY) ======
                    is_valid, reason = is_valid_stock_contract(ib, sym, valid_contracts=valid_contracts)
                    if not is_valid:
                        if sym not in invalid_symbols_logged:
                            print(f"[REJECT] {sym}: not tradeable ({reason})")
                            invalid_symbols_logged.add(sym)
                        invalid_symbols.add(sym)
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
                    if not math.isfinite(atr) or atr <= 0:
                        continue

                    # ====== CALCULATE SIZING ======
                    # Risk unit = 1 ATR (will be multiplied by STOP_LOSS_R for actual stop distance)
                    risk_dollars = equity * risk_per_trade_pct
                    qty = int(risk_dollars // atr)
                    if qty <= 0:
                        continue

                    # Calculate bracket levels:
                    # - entry: where we buy
                    # - stop_loss: hard downside protection (ATR-based risk management)
                    # - trail_amt: upside capture (follows price up to lock in gains)
                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    stop_loss = entry - (STOP_LOSS_R * atr)  # DOWN from entry
                    trail_amt = 0.0  # Delayed trailing stop activation
                    trail_activate_px = entry + (TRAIL_ACTIVATE_ATR * atr)
                    
                    # Triple-check before submission
                    if not math.isfinite(entry) or entry <= 0:
                        print(f"[VALIDATION] {sym}: entry price invalid (${entry})")
                        continue
                    
                    if not math.isfinite(stop_loss) or stop_loss <= 0:
                        print(f"[VALIDATION] {sym}: stop loss invalid (${stop_loss})")
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

                    # ====== CHECK THROTTLE: GLOBAL BRACKET COOLDOWN ======
                    if BRACKET_COOLDOWN_SECONDS > 0 and (now - last_bracket_attempt_ts) < BRACKET_COOLDOWN_SECONDS:
                        cooldown_remain = BRACKET_COOLDOWN_SECONDS - (now - last_bracket_attempt_ts)
                        print(f"[THROTTLE] Bracket cooldown active ({cooldown_remain:.0f}s remaining)")
                        break
                    
                    # ====== CHECK THROTTLE: MAX 1 PER LOOP ======
                    if brackets_submitted_this_loop >= MAX_NEW_BRACKETS_PER_LOOP:
                        print(f"[THROTTLE] {sym}: max {MAX_NEW_BRACKETS_PER_LOOP} bracket(s) per loop reached")
                        break
                    
                    # ====== SIM MODE ======
                    if not armed:
                        print(
                            f"[SIM] {sym} qty={qty} entry=${entry:.2f} stop_loss=${stop_loss:.2f} "
                            f"trail=PENDING (activate>${trail_activate_px:.2f}) risk={risk_per_trade_pct:.3%}"
                        )
                        last_bracket_attempt_ts = now
                        brackets_submitted_this_loop += 1
                        break

                    # ====== SUBMIT BRACKET (ARMED MODE) ======
                    params = BracketParams(
                        symbol=sym, qty=qty,
                        entry_limit=entry, stop_loss=stop_loss, trail_amount=trail_amt,
                        tif="DAY"
                    )
                    res = place_limit_tp_trail_bracket(ib, params)
                    print(f"[IB] {sym} -> {res.ok} {res.message}")
                    last_bracket_attempt_ts = now
                    brackets_submitted_this_loop += 1
                    
                    if res.ok:
                        active.add(sym)
                        last_bracket_ts[sym] = now
                        stop_order_ids[sym] = res.stop_id or 0
                        entry_atr_by_symbol[sym] = atr
                        if loss_streak_penalty_remaining > 0:
                            loss_streak_penalty_remaining -= 1

                    # After one attempt (success or fail), do not attempt more symbols this loop
                    break

                last_symbols = current_symbols
                last_print_ts = now

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, LOOP_SECONDS - elapsed))

    except KeyboardInterrupt:
        print("\nStopping live loop.")
    finally:
        # Clean up: give pending async tasks time to complete
        ib.sleep(0.2)
        
        # Cancel pending async tasks before disconnect
        import asyncio
        try:
            pending = asyncio.all_tasks()
            for task in pending:
                task.cancel()
        except:
            pass
        
        # Force close connection
        try:
            ib.client.disconnect()
        except:
            pass
        
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
