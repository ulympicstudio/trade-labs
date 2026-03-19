"""
Bus topic constants.

Centralises all Redis Pub/Sub channel names so publishers and
subscribers stay in sync.  Naming convention::

    tl.<arm>.<noun>

where *tl* is the trade-labs prefix.
"""

# ── Market data ──────────────────────────────────────────────────────
MARKET_SNAPSHOT = "tl.ingest.market_snapshot"
UNIVERSE_CANDIDATES = "tl.ingest.universe_candidates"

# ── News ─────────────────────────────────────────────────────────────
NEWS_EVENT = "tl.ingest.news_event"

# ── Signals / intents ────────────────────────────────────────────────
TRADE_INTENT = "tl.signal.trade_intent"
WATCH_CANDIDATE = "tl.signal.watch_candidate"
OPEN_PLAN_CANDIDATE = "tl.signal.open_plan_candidate"

# ── Risk → Execution ────────────────────────────────────────────────
ORDER_PLAN = "tl.risk.order_plan"
PLAN_DRAFT = "tl.risk.plan_draft"
ORDER_BLUEPRINT = "tl.risk.order_blueprint"
ORDER_PLAN_APPROVED = "tl.risk.order_plan_approved"   # risk-approved, ready to execute
ORDER_PLAN_REJECTED = "tl.risk.order_plan_rejected"   # risk-rejected, with reasons

# ── Execution events ────────────────────────────────────────────────
ORDER_EVENT = "tl.execution.order_event"

# ── Heartbeats ──────────────────────────────────────────────────────
HEARTBEAT = "tl.monitor.heartbeat"

# ── Convenience collection ──────────────────────────────────────────
ALL_TOPICS = (
    MARKET_SNAPSHOT,
    UNIVERSE_CANDIDATES,
    NEWS_EVENT,
    TRADE_INTENT,
    WATCH_CANDIDATE,
    OPEN_PLAN_CANDIDATE,
    ORDER_PLAN,
    PLAN_DRAFT,
    ORDER_BLUEPRINT,
    ORDER_PLAN_APPROVED,
    ORDER_PLAN_REJECTED,
    ORDER_EVENT,
    HEARTBEAT,
)
