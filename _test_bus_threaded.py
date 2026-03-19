"""Test bus delivery with threaded publishers/subscribers like dev_all_in_one."""
import threading
import time
from src.bus.local_bus import LocalBus
from src.bus.bus_factory import set_shared_bus, get_bus, clear_shared_bus
from src.bus.topics import MARKET_SNAPSHOT, NEWS_EVENT
from src.schemas.messages import MarketSnapshot, NewsEvent

clear_shared_bus()
bus = LocalBus()
set_shared_bus(bus)

received_snaps = []
received_news = []
pub_count = 0

def subscriber_thread():
    """Mimics signal arm: subscribe then wait."""
    b = get_bus()
    print(f"[sub] got bus, is_connected={b.is_connected}, same={b is bus}")
    b.subscribe(MARKET_SNAPSHOT, lambda s: received_snaps.append(s), msg_type=MarketSnapshot)
    b.subscribe(NEWS_EVENT, lambda n: received_news.append(n), msg_type=NewsEvent)
    print(f"[sub] subscribed")

def publisher_thread():
    """Mimics ingest arm: publish snapshots."""
    global pub_count
    b = get_bus()
    print(f"[pub] got bus, same={b is bus}")
    for i in range(5):
        snap = MarketSnapshot(symbol=f"SYM{i}", last=100.0+i, bid=99.9+i, ask=100.1+i)
        ok = b.publish(MARKET_SNAPSHOT, snap)
        if ok:
            pub_count += 1
        time.sleep(0.05)
    news = NewsEvent(symbol="AAPL", headline="Test news")
    b.publish(NEWS_EVENT, news)
    pub_count += 1
    print(f"[pub] published {pub_count}")

# Launch with stagger like dev_all_in_one
t_pub = threading.Thread(target=publisher_thread, daemon=True)
t_sub = threading.Thread(target=subscriber_thread, daemon=True)

t_pub.start()
time.sleep(0.2)  # same stagger as dev_all_in_one
t_sub.start()

t_sub.join(timeout=2)
t_pub.join(timeout=5)
time.sleep(1)  # let dispatcher catch up

print(f"Published: {pub_count}")
print(f"Received snaps: {len(received_snaps)}, news: {len(received_news)}")

# Now test with subscriber FIRST (correct order)
clear_shared_bus()
bus2 = LocalBus()
set_shared_bus(bus2)
received_snaps2 = []

def sub_first():
    b = get_bus()
    b.subscribe(MARKET_SNAPSHOT, lambda s: received_snaps2.append(s), msg_type=MarketSnapshot)
    print("[sub2] subscribed first")

def pub_after():
    time.sleep(0.3)  # wait for sub
    b = get_bus()
    for i in range(5):
        b.publish(MARKET_SNAPSHOT, MarketSnapshot(symbol=f"T{i}", last=float(i)))
    print("[pub2] published 5")

t2s = threading.Thread(target=sub_first, daemon=True)
t2p = threading.Thread(target=pub_after, daemon=True)
t2s.start()
t2p.start()
t2s.join(timeout=2)
t2p.join(timeout=5)
time.sleep(1)
print(f"Sub-first received: {len(received_snaps2)}")

if len(received_snaps) < 5:
    print(f"ISSUE: publisher-first lost {5 - len(received_snaps)} snaps (published before subscribe)")
if len(received_snaps2) == 5:
    print("PASS: subscriber-first receives all")
else:
    print(f"FAIL: subscriber-first got {len(received_snaps2)}")

bus.close()
bus2.close()
