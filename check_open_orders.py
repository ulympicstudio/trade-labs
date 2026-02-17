#!/usr/bin/env python3
"""
Check all open orders in TWS with detailed TRAIL info
"""
import os
os.environ['IB_CLIENT_ID_OVERRIDE'] = '32'

from ib_insync import IB

ib = IB()
try:
    print("\n=== Checking Open Orders in TWS ===\n")
    print("[CONNECT] Connecting to IB...")
    ib.connect('127.0.0.1', 7497, clientId=32, timeout=10)
    print("[CONNECT] Connected\n")
    
    # Give IB time to sync
    ib.sleep(0.5)
    
    # Get all open orders
    open_orders = ib.openOrders()
    print(f"[ORDERS] Total open orders in TWS: {len(open_orders)}\n")
    
    # Look for NVDA orders specifically
    nvda_orders = [t for t in open_orders if t.contract.symbol == 'NVDA']
    print(f"[NVDA] NVDA orders found: {len(nvda_orders)}\n")
    
    if nvda_orders:
        # Group by parent
        parents = {}
        for trade in nvda_orders:
            parentId = trade.order.parentId
            if parentId not in parents:
                parents[parentId] = []
            parents[parentId].append(trade)
        
        for parent_id, children in parents.items():
            if parent_id == 0:  # Parent order
                parent_trade = children[0]
                print(f"[ORDER] Parent (ID={parent_trade.order.orderId})")
                print(f"  Action: {parent_trade.order.action}")
                print(f"  Type: {parent_trade.order.orderType}")
                print(f"  Price: {parent_trade.order.lmtPrice}")
                print(f"  Status: {parent_trade.orderStatus}")
            else:  # Child orders
                for child in children:
                    order = child.order
                    print(f"[ORDER] Child (ID={order.orderId}, Parent={order.parentId})")
                    print(f"  Action: {order.action}")
                    print(f"  Type: {order.orderType}")
                    if order.orderType == "LIMIT":
                        print(f"  Price: {order.lmtPrice}")
                    elif order.orderType == "TRAIL":
                        print(f"  Trail Amt: ${order.auxPrice}")
                    print(f"  OCA: {order.ocaGroup}")
                    print(f"  Status: {child.orderStatus}")
    else:
        print("[INFO] No NVDA orders currently open.")
        print("[INFO] If Error 399 warnings, orders are pending market open.\n")
    
    # Summary
    print("\n[ANALYSIS]")
    print("  If TRAIL child is NOT showing:")
    print("    1. Orders may be after-hours (wait for 9:30 AM ET market open)")
    print("    2. Refresh TWS window (F5)")
    print("    3. Check IB logs for rejection reasons")
    print("\n  If all 3 legs ARE showing:")
    print("    ✓ System is working correctly!")
    print("    ✓ TRAIL order is ready to execute when parent fills\n")
    
finally:
    ib.disconnect()

