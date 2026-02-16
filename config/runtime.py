 
import os

def mode() -> str:
    return os.getenv("TRADE_LABS_MODE", "PAPER").upper()

def is_paper() -> bool:
    return mode() == "PAPER"

def execution_backend() -> str:
    # SIM (default) or IB
    return os.getenv("TRADE_LABS_EXECUTION_BACKEND", "SIM").upper()

def is_armed() -> bool:
    # Must be EXACTLY "1" to allow broker submission
    return os.getenv("TRADE_LABS_ARMED", "0") == "1"

