import os
import sys

# Add project root to sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.signals.spy_signal import generate_intents


def get_trade_intents():
    """
    Single entry point for Studio's 'brain.'
    Later we will swap this to real scanners and scoring models.
    """
    return generate_intents()
