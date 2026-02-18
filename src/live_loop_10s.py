from dotenv import load_dotenv
load_dotenv()

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
from src.risk.regime import get_regime, RegimeResult
from src.signals.signal_validator import (
    compute_candidate_metrics,
    passes_hyper_swing_filters,
    CandidateMetrics,
    fetch_spy_5m,
)
from src.quant.hyper_swing_filters import calc_momentum
from config.risk_limits import (
    MIN_UNIFIED_SCORE,
    MIN_ADV20_DOLLARS,
    MIN_ATR_PCT,
    MIN_VOLUME_ACCEL,
    MIN_RS_VS_SPY,
    PRICE_MIN as CFG_PRICE_MIN,
    PRICE_MAX as CFG_PRICE_MAX,
    PRICE_MAX_ALLOWLIST,
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
MIN_CATALYST_SCORE = 60.0  # Catalyst score threshold for trading (tuned for higher candidate flow)

# Phase 2: Unified score weights
UNIFIED_CATALYST_WEIGHT = 0.60
UNIFIED_QUANT_WEIGHT    = 0.40
# Regime-based risk scaling
YELLOW_RISK_REDUCTION   = 0.75   # 25% risk haircut in YELLOW regime
YELLOW_POSITION_PENALTY = 1      # Reduce max positions by 1

LOSS_STREAK_LOOKBACK = 2
LOSS_STREAK_PENALTY_TRADES = 3
LOSS_STREAK_RISK_REDUCTION = 0.75

PEAK_DRAWDOWN_THRESHOLD = 0.05
PEAK_DRAWDOWN_RISK_PCT = 0.004
# ---- Loop Settings ----
LOOP_SECONDS = 10
SCAN_REFRESH_SECONDS = 300
SCAN_LIMIT = 60
SCORE_TOP_N_FROM_SCAN = 20
TRADE_TOP_N = 12  # Top N scored candidates to evaluate (was 6, show more variety)
CATALYST_TOP_N = 8
MIN_CATALYSTS_FOR_SCAN = 3
CATALYST_POOL_SIZE = 20

ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.5  # How many ATRs below entry is our hard stop
TRAIL_ATR_MULT = 1.2
TRAIL_ACTIVATE_ATR = 1.5
TRAIL_CHECK_SECONDS = 30

MIN_PRICE = 2.0
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
MAX_NEW_BRACKETS_PER_LOOP = 2           # Submit max 2 brackets per 10s loop
COOLDOWN_SECONDS_PER_SYMBOL = 300       # Don't retry same symbol for 5 min
BRACKET_COOLDOWN_SECONDS = int(os.getenv("TRADE_LABS_BRACKET_COOLDOWN_SECONDS", "20"))
HYPER_RECHECK_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_SECONDS", "30"))
HYPER_RECHECK_VOL_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_VOL_SECONDS", str(HYPER_RECHECK_SECONDS)))
HYPER_RECHECK_VWAP_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_VWAP_SECONDS", str(HYPER_RECHECK_SECONDS)))
HYPER_RECHECK_PRICECAP_SECONDS = int(os.getenv("TRADE_LABS_HYPER_RECHECK_PRICECAP_SECONDS", "900"))
ENABLE_BOUNCE_MODE = os.getenv("TRADE_LABS_ENABLE_BOUNCE_MODE", "1") == "1"
BOUNCE_MIN_PLAYBOOK_WIN_RATE = float(os.getenv("TRADE_LABS_BOUNCE_MIN_WIN_RATE", "0.0"))
BOUNCE_MIN_PLAYBOOK_EXPECTANCY = float(os.getenv("TRADE_LABS_BOUNCE_MIN_EXPECTANCY", "-0.005"))
BOUNCE_MIN_PLAYBOOK_SCORE = float(os.getenv("TRADE_LABS_BOUNCE_MIN_SCORE", "25"))
BOUNCE_MAX_MAE_ABS = float(os.getenv("TRADE_LABS_BOUNCE_MAX_MAE_ABS", "0.09"))
MIN_BOUNCE_SAMPLE_SIZE_FLOOR = 12
BASE_MIN_UNIFIED_SCORE_FLOOR = 70.0
CONVICTION_MIN_UNIFIED_SCORE_FLOOR = 68.0
BOUNCE_MIN_UNIFIED_SCORE_FLOOR = 68.0
BOUNCE_MIN_SAMPLE_SIZE = max(
    MIN_BOUNCE_SAMPLE_SIZE_FLOOR,
    int(os.getenv("TRADE_LABS_BOUNCE_MIN_SAMPLE_SIZE", str(MIN_BOUNCE_SAMPLE_SIZE_FLOOR))),
)
BASE_MIN_UNIFIED_SCORE = max(
    BASE_MIN_UNIFIED_SCORE_FLOOR,
    float(os.getenv("TRADE_LABS_BASE_MIN_UNIFIED_SCORE", str(float(MIN_UNIFIED_SCORE)))),
)
CONVICTION_MIN_UNIFIED_SCORE = max(
    CONVICTION_MIN_UNIFIED_SCORE_FLOOR,
    float(os.getenv("TRADE_LABS_CONVICTION_MIN_UNIFIED_SCORE", "68")),
)
BOUNCE_MIN_UNIFIED_SCORE = max(
    BOUNCE_MIN_UNIFIED_SCORE_FLOOR,
    float(os.getenv("TRADE_LABS_BOUNCE_MIN_UNIFIED_SCORE", "68")),
)

# Session-level invalid symbol cache (to suppress repeated IB errors)
INVALID_SYMBOL_CACHE: Set[str] = set()

# Delayed trailing state tracking
TRAIL_STATE: Dict[str, str] = {}  # symbol -> "pending" | "activated"
TRAIL_LOGGED: Set[str] = set()  # session-level dedup for activation logs

# Symbol-level order lock: prevent duplicate entries
SYMBOL_COOLDOWN_SECONDS = int(os.getenv("TRADE_LABS_SYMBOL_COOLDOWN_SECONDS", "900"))
symbol_lock_ts: Dict[str, float] = {}  # symbol -> timestamp of last bracket attempt


def is_symbol_locked(ib: IB, sym: str, symbol_lock_ts: Dict[str, float], now: float, cooldown: int) -> Tuple[bool, str]:
    """
    Check if a symbol is locked (can't place new bracket).
    
    Returns (locked: bool, reason: str):
    - (True, "position_open") if symbol has non-zero position
    - (True, "open_trade_status=...") if symbol in openTrades with non-terminal status
    - (True, "trail_state_present") if symbol in TRAIL_STATE dict
    - (True, "cooldown_active(...s)") if recent attempt within cooldown window
    - (False, "unlocked") if available
    """
    # Check 1: Active position
    try:
        for pos in ib.positions():
            if pos.contract.symbol == sym and pos.position != 0:
                return True, "position_open"
    except Exception as e:
        pass  # Silent fail on IB error
    
    # Check 2: Open trades (check status)
    try:
        for trade in ib.openTrades():
            if trade.contract.symbol == sym:
                st = getattr(trade.orderStatus, "status", "")
                if st not in ("Filled", "Cancelled", "Inactive"):
                    return True, f"open_trade_status={st}"
    except Exception as e:
        pass  # Silent fail
    
    # Check 3: Trail state (symbol already has pending/active trail)
    if sym in TRAIL_STATE:
        return True, "trail_state_present"
    
    # Check 4: Recent bracket attempt cooldown
    last = symbol_lock_ts.get(sym)
    if last and (now - last) < cooldown:
        remaining = int(cooldown - (now - last))
        return True, f"cooldown_active({remaining}s)"
    
    return False, "unlocked"


def connect_ib() -> IB:
    ib = IB()
    ib.RequestTimeout = 5

    fallback_span = max(1, int(os.getenv("TRADE_LABS_IB_CLIENT_ID_SPAN", "10")))
    last_error: Optional[Exception] = None
    connected_client_id: Optional[int] = None

    for offset in range(fallback_span):
        client_id = IB_CLIENT_ID + offset
        try:
            ib.connect(IB_HOST, IB_PORT, clientId=client_id, timeout=10)
            connected_client_id = client_id
            break
        except Exception as e:
            last_error = e
            msg = str(e)
            if "already in use" in msg.lower() or "326" in msg:
                print(f"[IB] clientId {client_id} busy, trying next...")
            else:
                print(f"[IB] connect failed with clientId {client_id}: {e}")
            try:
                ib.disconnect()
            except Exception:
                pass

    if connected_client_id is None:
        if last_error is not None:
            raise last_error
        raise ConnectionError("Unable to connect to IB: no available clientId")

    if connected_client_id != IB_CLIENT_ID:
        print(f"[IB] Connected using fallback clientId={connected_client_id} (base={IB_CLIENT_ID})")

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
                # Enhanced ETF/product filter — word-boundary regex to avoid
                # false positives (e.g. NETFLIX does not contain \bETF\b).
                _ETF_PATTERN = (
                    r"\bETF\b|\bETN\b|\bTRUST\b|\bFUND\b|\bINDEX\b|\bNOTE\b|\bNOTES\b"
                    r"|\bSECURITIES\b|\bULTRA\b|\bPROSHARES\b|\b2X\b|\b3X\b"
                    r"|\bLEVERAGED\b|\bINVERSE\b"
                )
                if re.search(_ETF_PATTERN, long_name):
                    matched = re.search(_ETF_PATTERN, long_name).group()
                    return False, f"longName matched '{matched}'"
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
    if ENABLE_BOUNCE_MODE:
        print(
            "[BOUNCE_CFG] "
            f"min_unified={BOUNCE_MIN_UNIFIED_SCORE:.1f} "
            f"base(wr={BOUNCE_MIN_PLAYBOOK_WIN_RATE*100:.1f}% score={BOUNCE_MIN_PLAYBOOK_SCORE:.1f} mae<={BOUNCE_MAX_MAE_ABS*100:.1f}%) "
            f"requires_n>={BOUNCE_MIN_SAMPLE_SIZE}"
        )
    print(
        f"[SCORE_CFG] base_min={BASE_MIN_UNIFIED_SCORE:.1f} "
        f"conviction_min={CONVICTION_MIN_UNIFIED_SCORE:.1f} "
        f"bounce_min={BOUNCE_MIN_UNIFIED_SCORE:.1f}"
    )

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
            if getattr(hunter, "finnhub_key", None):
                print("✅ [FINNHUB] configured")
            else:
                print("⚠️  [FINNHUB] missing/placeholder key; source will be skipped")
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
    last_hyper_reject_ts: Dict[str, float] = {}  # symbol -> timestamp of most recent hyper-filter reject
    last_hyper_reject_reason: Dict[str, str] = {}  # symbol -> last hyper-filter reject reason
    stop_order_ids: Dict[str, int] = {}
    entry_atr_by_symbol: Dict[str, float] = {}
    trail_active_symbols: Set[str] = set()
    catalyst_rotation = 0
    scanner_rotation = 0

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
    force_kill_triggered = False

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

            force_kill = os.getenv("TRADE_LABS_FORCE_KILL") == "1" and not force_kill_triggered
            if force_kill or is_kill_switch_active(ib):
                if not trading_halted_for_day:
                    if force_kill:
                        print("[KILL_SWITCH] Forced trigger via TRADE_LABS_FORCE_KILL=1.")
                        force_kill_triggered = True
                        try:
                            os.environ.pop("TRADE_LABS_FORCE_KILL", None)
                        except:
                            pass
                    print("[KILL_SWITCH] Triggered. Canceling orders and flattening positions.")
                    try:
                        print("[KILL_SWITCH] Canceling all open orders...")
                        ib.reqGlobalCancel()
                    except Exception:
                        pass
                    print("[KILL_SWITCH] Flattening positions...")
                    close_positions_by_weakness(ib, armed)
                    trading_halted_for_day = True
                    halted_date = current_session_date
                    print("[KILL_SWITCH] Trading halted for the day.")
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
                    catalyst_ranking = research_engine.scorer.rank_opportunities(
                        catalyst_hunt_results,
                        max_results=CATALYST_POOL_SIZE,
                    )
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
                if len(catalyst_ranking) > 0:
                    rot = catalyst_rotation % len(catalyst_ranking)
                    ranked_view = catalyst_ranking[rot:] + catalyst_ranking[:rot]
                else:
                    ranked_view = catalyst_ranking
                
                for opp in ranked_view[:CATALYST_TOP_N]:
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
                if catalyst_ranking:
                    catalyst_rotation = (catalyst_rotation + 1) % len(catalyst_ranking)
            
            # Scanner supplement: always reserve diversity slots beyond catalyst picks
            if (now - last_scan_score_ts) >= SCAN_REFRESH_SECONDS or not last_scanner_scored:
                scan_for_scoring = cached_scan[:SCORE_TOP_N_FROM_SCAN]
                last_scanner_scored = score_scan_results(ib, scan_for_scoring, top_n=TRADE_TOP_N)
                last_scan_score_ts = now

            scanner_scored = last_scanner_scored
            if scanner_scored:
                rot = scanner_rotation % len(scanner_scored)
                scanner_ranked_view = scanner_scored[rot:] + scanner_scored[:rot]
                scanner_rotation = (scanner_rotation + 1) % len(scanner_scored)
            else:
                scanner_ranked_view = scanner_scored

            # Dedup: don't include scanner results already in catalyst picks
            catalyst_syms = set(s.symbol for s in scored)
            scanner_only = [s for s in scanner_ranked_view if s.symbol not in catalyst_syms]

            scanner_slots = max(0, TRADE_TOP_N - len(scored))
            scanner_added = scanner_only[:scanner_slots]
            scored.extend(scanner_added)

            if scanner_added:
                print(f"  [SCANNER] Added {len(scanner_added)} diversity candidates")

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

            # ====== REGIME FILTER (Phase 2) ======
            regime = get_regime(ib, breadth_pct=last_breadth_pct)

            breadth_trigger = (last_breadth_pct is not None) and (last_breadth_pct >= BREADTH_ADV_THRESHOLD)
            conviction_mode = (
                (high_score_count >= 3
                 or earnings_high_conf_count >= 2
                 or breadth_trigger)
                and regime.regime != "RED"   # Never convict in RED
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

            # Regime scaling
            if regime.regime == "YELLOW":
                risk_per_trade_pct *= YELLOW_RISK_REDUCTION
                max_concurrent_positions = max(1, max_concurrent_positions - YELLOW_POSITION_PENALTY)

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

            # Pre-compute SPY momentum once for all candidates
            _spy_df = fetch_spy_5m(ib)
            _spy_mom_30m = calc_momentum(_spy_df, 30) if _spy_df is not None else 0.0

            if should_print:
                engine_status = "🎯 CATALYST PRIMARY" if research_engine else "📊 SCANNER"
                if last_breadth_pct is not None:
                    breadth_pct_display = f"{last_breadth_pct*100:.1f}% ({last_breadth_source})"
                else:
                    breadth_pct_display = "n/a"
                mode_label = "CONVICTION" if conviction_mode else "BASE"
                conviction_audit = f" [{', '.join(conviction_reasons)}]" if conviction_mode else ""
                regime_tag = f"regime={regime.regime}"
                if regime.reasons:
                    regime_tag += f" [{', '.join(regime.reasons[:2])}]"
                print(
                    f"\n--- Loop --- ARMED={armed} mode={mode_label}{conviction_audit} {regime_tag} "
                    f"equity={equity:,.0f} open_risk={open_risk_pct:.3f} active={len(active)} "
                    f"breadth={breadth_pct_display} {engine_status}"
                )

                brackets_submitted_this_loop = 0  # Throttle: max 1 per loop
                
                # ====== REGIME GATE: RED → skip new entries ======
                if regime.regime == "RED":
                    print(f"[REGIME] RED — no new entries. Reasons: {', '.join(regime.reasons)}")

                for cand in scored:
                    sym = cand.symbol

                    if sym in invalid_symbols:
                        continue

                    # Skip if already active
                    if sym in active:
                        continue

                    # Skip recently hyper-rejected symbols to avoid re-check spam every loop
                    last_hs_reject = last_hyper_reject_ts.get(sym, 0.0)
                    last_hs_reason = last_hyper_reject_reason.get(sym, "")
                    recheck_seconds = HYPER_RECHECK_SECONDS
                    if "price below VWAP" in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_VWAP_SECONDS
                    elif "vol_accel" in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_VOL_SECONDS
                    elif " > max " in last_hs_reason:
                        recheck_seconds = HYPER_RECHECK_PRICECAP_SECONDS

                    if (now - last_hs_reject) < recheck_seconds:
                        continue

                    # Regime RED: block new entries entirely
                    if regime.regime == "RED":
                        break

                    # Skip if max positions reached
                    if len(active) >= max_concurrent_positions:
                        print("Max concurrent positions reached.")
                        break

                    # ====== SCORE THRESHOLD CHECK (CATALYST ONLY) ======
                    cat_score = getattr(cand, 'catalyst_score', None) or 0.0
                    if cat_score < MIN_CATALYST_SCORE:
                        continue

                    # ====== FIX B: UNIVERSE FILTER (STOCKS ONLY) ======
                    is_valid, reason = is_valid_stock_contract(ib, sym, valid_contracts=valid_contracts)
                    if not is_valid:
                        if sym not in invalid_symbols_logged:
                            print(f"[REJECT] {sym}: not tradeable ({reason})")
                            invalid_symbols_logged.add(sym)
                        invalid_symbols.add(sym)
                        continue

                    # ====== QUANT VERIFICATION (Phase 2) ======
                    try:
                        qm = compute_candidate_metrics(ib, sym, spy_mom_30m=_spy_mom_30m)
                    except Exception as e:
                        print(f"[SKIP] {sym}: quant metrics failed ({e})")
                        continue

                    if not qm.ok:
                        print(f"[SKIP] {sym}: {qm.error}")
                        continue

                    # ---- Hyper-swing gates ----
                    hs_cfg = dict(
                        PRICE_MIN=CFG_PRICE_MIN,
                        PRICE_MAX=CFG_PRICE_MAX,
                        MIN_ATR_PCT=MIN_ATR_PCT,
                        MIN_ADV20_DOLLARS=MIN_ADV20_DOLLARS,
                        MIN_VOLUME_ACCEL=MIN_VOLUME_ACCEL,
                        MIN_RS_VS_SPY=MIN_RS_VS_SPY,
                        REQUIRE_ABOVE_VWAP=True,
                        PRICE_MAX_ALLOWLIST=PRICE_MAX_ALLOWLIST,
                    )
                    hs_pass, hs_reason = passes_hyper_swing_filters(qm, config=hs_cfg)
                    gate_tag = "MOMENTUM"
                    gate_reason = hs_reason
                    gate_pass = hs_pass

                    bounce_pass = False
                    bounce_reason = ""
                    if not hs_pass and ENABLE_BOUNCE_MODE and regime.regime in ("GREEN", "YELLOW"):
                        bounce_reject_reasons = []
                        playbook_sample = max(0, int(getattr(qm, "playbook_sample_size_5d", 0) or 0))

                        if playbook_sample < BOUNCE_MIN_SAMPLE_SIZE:
                            bounce_reject_reasons.append(
                                f"playbook_n {playbook_sample} < min {BOUNCE_MIN_SAMPLE_SIZE} (informational only)"
                            )

                        if qm.price < CFG_PRICE_MIN:
                            bounce_reject_reasons.append(f"price ${qm.price:.2f} < min ${CFG_PRICE_MIN}")
                        if qm.price > CFG_PRICE_MAX and sym not in PRICE_MAX_ALLOWLIST:
                            bounce_reject_reasons.append(f"price ${qm.price:.2f} > max ${CFG_PRICE_MAX}")
                        if qm.atr_percent < MIN_ATR_PCT:
                            bounce_reject_reasons.append(f"atr% {qm.atr_percent:.3f} < {MIN_ATR_PCT}")
                        if qm.adv20_dollars < MIN_ADV20_DOLLARS:
                            bounce_reject_reasons.append(
                                f"adv20 ${qm.adv20_dollars/1e6:.1f}M < ${MIN_ADV20_DOLLARS/1e6:.0f}M"
                            )
                        if qm.playbook_win_rate_5d < BOUNCE_MIN_PLAYBOOK_WIN_RATE:
                            bounce_reject_reasons.append(
                                f"playbook_wr {qm.playbook_win_rate_5d*100:.1f}% < {BOUNCE_MIN_PLAYBOOK_WIN_RATE*100:.1f}%"
                            )
                        if qm.playbook_expectancy_5d < BOUNCE_MIN_PLAYBOOK_EXPECTANCY:
                            bounce_reject_reasons.append(
                                f"playbook_exp {qm.playbook_expectancy_5d*100:+.2f}% < {BOUNCE_MIN_PLAYBOOK_EXPECTANCY*100:+.2f}%"
                            )
                        if qm.playbook_score < BOUNCE_MIN_PLAYBOOK_SCORE:
                            bounce_reject_reasons.append(
                                f"playbook_score {qm.playbook_score:.1f} < {BOUNCE_MIN_PLAYBOOK_SCORE:.1f}"
                            )
                        if abs(min(0.0, qm.playbook_mae_5d)) > BOUNCE_MAX_MAE_ABS:
                            bounce_reject_reasons.append(
                                f"playbook_mae {qm.playbook_mae_5d*100:+.2f}% worse than -{BOUNCE_MAX_MAE_ABS*100:.2f}%"
                            )

                        if not bounce_reject_reasons:
                            bounce_pass = True
                            bounce_reason = (
                                f"[BOUNCE_PASS] {sym}: "
                                f"wr={qm.playbook_win_rate_5d*100:.1f}% exp={qm.playbook_expectancy_5d*100:+.2f}% "
                                f"score={qm.playbook_score:.1f} mae={qm.playbook_mae_5d*100:+.2f}% n={playbook_sample} "
                                f"atr%={qm.atr_percent*100:.2f}% adv=${qm.adv20_dollars/1e6:.0f}M regime={regime.regime}"
                            )
                        else:
                            bounce_reason = f"[BOUNCE_REJECT] {' | '.join(bounce_reject_reasons)}"

                    if not hs_pass and bounce_pass:
                        gate_tag = "BOUNCE"
                        gate_reason = bounce_reason
                        gate_pass = True

                    if not gate_pass:
                        last_hyper_reject_ts[sym] = now
                        last_hyper_reject_reason[sym] = f"{hs_reason} || {bounce_reason}" if bounce_reason else hs_reason
                        print(f"[HYPER_FILTER] {sym}: {hs_reason}")
                        if bounce_reason:
                            print(f"[BOUNCE_FILTER] {sym}: {bounce_reason}")
                        continue
                    last_hyper_reject_ts.pop(sym, None)
                    last_hyper_reject_reason.pop(sym, None)
                    print(gate_reason)

                    # ---- Unified score ----
                    unified = (UNIFIED_CATALYST_WEIGHT * cat_score
                               + UNIFIED_QUANT_WEIGHT * qm.quant_score)
                    if gate_tag == "BOUNCE":
                        required_unified = BOUNCE_MIN_UNIFIED_SCORE
                    else:
                        required_unified = CONVICTION_MIN_UNIFIED_SCORE if conviction_mode else BASE_MIN_UNIFIED_SCORE
                    if unified < required_unified:
                        print(
                            f"[SCORE] {sym}: unified={unified:.1f} < {required_unified:.1f} "
                            f"(mode={gate_tag} cat={cat_score:.0f} quant={qm.quant_score:.0f})"
                        )
                        continue

                    # ---- Candidate summary ----
                    print(
                        f"  ✅ [{gate_tag}] {sym}  unified={unified:.1f} cat={cat_score:.0f} quant={qm.quant_score:.0f}  "
                        f"mom30={qm.momentum_30m*100:+.2f}% vol_accel={qm.volume_accel:.2f} "
                        f"RS30mΔ={qm.rel_strength_vs_spy*100:+.2f}% atr%={qm.atr_percent*100:.2f}% "
                        f"adv=${qm.adv20_dollars/1e6:.0f}M playbook={qm.playbook_score:.0f} n={qm.playbook_sample_size_5d}"
                    )

                    # ====== USE QUANT METRICS for price/ATR ======
                    px = qm.price
                    atr = qm.atr14
                    # Sync ATR cache from quant verification
                    if atr > 0:
                        atr_cache[sym] = atr
                    if not math.isfinite(atr) or atr <= 0:
                        continue

                    # ====== CALCULATE SIZING ======
                    risk_dollars = equity * risk_per_trade_pct
                    qty = int(risk_dollars // atr)
                    if qty <= 0:
                        continue

                    entry = px * (1 - ENTRY_OFFSET_PCT)
                    stop_loss = entry - (STOP_LOSS_R * atr)
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
                    
                    # ====== CHECK SYMBOL LOCK: NO DUPLICATE ENTRIES ======
                    locked, lock_reason = is_symbol_locked(ib, sym, symbol_lock_ts, now, SYMBOL_COOLDOWN_SECONDS)
                    if locked:
                        print(f"[LOCK] {sym}: {lock_reason}, skipping")
                        continue
                    
                    # ====== SIM MODE ======
                    if not armed:
                        print(
                            f"[SIM] {sym} unified={unified:.0f} qty={qty} entry=${entry:.2f} stop=${stop_loss:.2f} "
                            f"trail=PENDING (>{trail_activate_px:.2f}) risk={risk_per_trade_pct:.3%}"
                        )
                        last_bracket_attempt_ts = now
                        symbol_lock_ts[sym] = now  # Lock immediately after attempt
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
                    symbol_lock_ts[sym] = now  # Lock immediately after attempt (success or fail)
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
