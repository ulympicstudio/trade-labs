"""Shared pytest fixtures and import-path setup for the mocked-IBKR suite."""

import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable (config/, src/).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def armed_paper(monkeypatch):
    """Arm the system in PAPER mode so bracket placement is permitted."""
    monkeypatch.setenv("TRADE_LABS_MODE", "PAPER")
    monkeypatch.setenv("TRADE_LABS_ARMED", "1")
    yield


@pytest.fixture
def disarmed(monkeypatch):
    """Disarm so bracket placement must refuse."""
    monkeypatch.setenv("TRADE_LABS_MODE", "PAPER")
    monkeypatch.setenv("TRADE_LABS_ARMED", "0")
    yield
