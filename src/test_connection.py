"""Simple script to test connection to Interactive Brokers using ib_insync."""
import os
import sys
from ib_insync import IB

# Ensure project root and src are on sys.path so `config` and `utils` packages import correctly
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID
from utils.logging import setup_logging


logger = setup_logging(__name__)


def main():
    ib = IB()
    try:
        logger.info(f"Connecting to IB at {IB_HOST}:{IB_PORT} clientId={IB_CLIENT_ID}")
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=5)
        print("Connected:", ib.isConnected())
        print("Accounts:", ib.managedAccounts())
    except Exception as exc:
        logger.exception("Connection failed")
        print("Connection failed:", exc)
        sys.exit(1)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
