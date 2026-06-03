"""P0 cooldown semantics + funnel de-nesting (monolith live loop).

These tests assert the *structure* of the live-loop entry path, because the
full main() cannot run without a broker/data feed. We verify:

  - Cooldown is LOSS-triggered only (winning/flat exits never arm it).
  - The daily kill-switch still gates entries.
  - Candidate evaluation is de-nested from the print cadence (runs every loop).
  - The blanket global/per-symbol cooldowns were removed.
"""

import ast
import inspect

import pytest

from src.risk.daily_pnl_manager import is_kill_switch_active
from tests.fake_ib import FakeIB


@pytest.fixture
def live_loop(monkeypatch):
    monkeypatch.setenv("TRADE_LABS_MODE", "PAPER")
    import src.live_loop_10s as L
    return L


def _main_src(L):
    return inspect.getsource(L.main)


# ── Cooldown: loss-triggered only ────────────────────────────────────────────

def test_cooldown_armed_only_on_losing_exit(live_loop):
    src = _main_src(live_loop)
    # The arming block must guard on pnl < 0.
    assert "loss_cooldown_until[_sym] = time.time() + LOSS_COOLDOWN_SECONDS" in src
    assert '(_ct.get("pnl") or 0.0) < 0' in src


def test_no_blanket_cooldown_constants(live_loop):
    """The removed blanket cooldowns must not reappear."""
    L = live_loop
    assert not hasattr(L, "BRACKET_COOLDOWN_SECONDS")
    assert not hasattr(L, "COOLDOWN_SECONDS_PER_SYMBOL")
    assert hasattr(L, "LOSS_COOLDOWN_SECONDS")


def test_winning_exit_does_not_arm_cooldown(live_loop):
    """Replay the arming logic: only losing pnl arms a per-symbol cooldown."""
    import time as _time
    LOSS_COOLDOWN_SECONDS = live_loop.LOSS_COOLDOWN_SECONDS
    loss_cooldown_until = {}
    seen = set()
    closed_trades = [
        {"symbol": "WIN", "exit_ts": "t1", "pnl": 50.0},
        {"symbol": "FLAT", "exit_ts": "t2", "pnl": 0.0},
        {"symbol": "LOSS", "exit_ts": "t3", "pnl": -25.0},
    ]
    for _ct in closed_trades:
        _key = f"{_ct.get('symbol')}|{_ct.get('exit_ts') or _ct.get('ts') or _ct.get('pnl')}"
        if _key in seen:
            continue
        seen.add(_key)
        _sym = _ct.get("symbol")
        if _sym and (_ct.get("pnl") or 0.0) < 0:
            loss_cooldown_until[_sym] = _time.time() + LOSS_COOLDOWN_SECONDS

    assert "WIN" not in loss_cooldown_until
    assert "FLAT" not in loss_cooldown_until
    assert "LOSS" in loss_cooldown_until
    assert loss_cooldown_until["LOSS"] > _time.time()


def test_closed_trade_keys_dedupe(live_loop):
    """A trade already seen must not re-arm cooldown on the next loop."""
    import time as _time
    loss_cooldown_until = {}
    seen = set()
    closed = [{"symbol": "LOSS", "exit_ts": "t3", "pnl": -25.0}]

    def _replay():
        for _ct in closed:
            _key = f"{_ct.get('symbol')}|{_ct.get('exit_ts') or _ct.get('ts') or _ct.get('pnl')}"
            if _key in seen:
                continue
            seen.add(_key)
            _sym = _ct.get("symbol")
            if _sym and (_ct.get("pnl") or 0.0) < 0:
                loss_cooldown_until[_sym] = _time.time() + 1.0

    _replay()
    first = loss_cooldown_until["LOSS"]
    loss_cooldown_until["LOSS"] = 0.0  # simulate expiry
    _replay()  # same key already seen → must NOT re-arm
    assert loss_cooldown_until["LOSS"] == 0.0


# ── Kill-switch still gates entries ───────────────────────────────────────────

def test_kill_switch_check_present_in_entry_path(live_loop):
    src = _main_src(live_loop)
    assert "if is_kill_switch_active(ib):" in src
    assert "kill_switch_daily_loss" in src


def test_kill_switch_runtime():
    """With no session-start equity recorded, the kill-switch is inactive."""
    ib = FakeIB()
    assert is_kill_switch_active(ib) is False


# ── Funnel: candidate evaluation de-nested from print cadence ─────────────────

def test_candidate_loop_not_nested_under_should_print(live_loop):
    """`for cand in scored:` must NOT be inside an `if should_print:` block."""
    src = _main_src(live_loop)
    tree = ast.parse(src)

    # Locate the `for cand in scored` loop and walk its ancestors: none of the
    # enclosing `if` statements may test `should_print`.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) \
                and node.target.id == "cand":
            if isinstance(node.iter, ast.Name) and node.iter.id == "scored":
                target = node
                break
    assert target is not None, "could not find `for cand in scored` loop"

    cur = target
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.If):
            test_src = ast.dump(cur.test)
            assert "should_print" not in test_src, (
                "candidate loop is gated by should_print — throughput bug"
            )


def test_brackets_per_loop_throttle_present(live_loop):
    src = _main_src(live_loop)
    assert "brackets_submitted_this_loop >= MAX_NEW_BRACKETS_PER_LOOP" in src


def test_concurrent_position_bounds(live_loop):
    L = live_loop
    assert L.BASE_MAX_CONCURRENT_POSITIONS == 10
    assert L.CONVICTION_MAX_CONCURRENT_POSITIONS == 12
    assert L.CONVICTION_MAX_CONCURRENT_POSITIONS >= L.BASE_MAX_CONCURRENT_POSITIONS
