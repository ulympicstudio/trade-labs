"""Quick smoke test: does LocalBus deliver messages?"""
from src.bus.local_bus import LocalBus
from src.bus.bus_factory import set_shared_bus, get_bus
from src.bus.topics import MARKET_SNAPSHOT, NEWS_EVENT
from src.schemas.messages import MarketSnapshot, NewsEvent
import time

bus = LocalBus()
set_shared_bus(bus)

received_snaps = []
received_news = []

def on_snap(s):
    received_snaps.append(s)
    print(f"  handler got snap: {s.symbol} last={s.last}")

def on_news(n):
    received_news.append(n)
    print(f"  handler got news: {n.symbol} hl={n.headline[:40]}")

# Simulate signal subscribing
b = get_bus()
b.subscribe(MARKET_SNAPSHOT, on_snap, msg_type=MarketSnapshot)
b.subscribe(NEWS_EVENT, on_news, msg_type=NewsEvent)
print(f"Subscribed. bus is same instance: {b is bus}")
print(f"Handlers registered: {list(bus._handlers.keys())}")

# Simulate ingest publishing
snap = MarketSnapshot(symbol="AAPL", last=150.0, bid=149.99, ask=150.01, volume=1000)
ok = bus.publish(MARKET_SNAPSHOT, snap)
print(f"Published snap ok={ok}")

news = NewsEvent(symbol="TSLA", headline="Tesla earnings beat expectations")
ok2 = bus.publish(NEWS_EVENT, news)
print(f"Published news ok={ok2}")

time.sleep(1)
print(f"Received: snaps={len(received_snaps)} news={len(received_news)}")
assert len(received_snaps) == 1, f"Expected 1 snap, got {len(received_snaps)}"
assert len(received_news) == 1, f"Expected 1 news, got {len(received_news)}"
print("PASS: bus pipeline works")
bus.close()
