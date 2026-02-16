print("Starting MVP run...")
import os

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.signals.test_signal_engine import main as run_signal_engine_test


def main():
    # Force safe defaults every run
    os.environ["TRADE_LABS_MODE"] = "PAPER"
    os.environ["TRADE_LABS_EXECUTION_BACKEND"] = "IB"

    print("\n----------------------------------------")
    print(f"{SYSTEM_NAME} → {HUMAN_NAME}")
    print("Running full MVP pipeline (REAL IB PAPER)")
    print("----------------------------------------\n")

    run_signal_engine_test()

    print("\nMVP run complete.\n")


if __name__ == "__main__":
    main()
