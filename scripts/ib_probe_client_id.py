from ib_insync import IB
import sys

# Try a range of client IDs to find one that works
for client_id in range(10, 100):
    ib = IB()
    try:
        print(f"Trying client ID: {client_id}")
        ib.connect('127.0.0.1', 7497, clientId=client_id, timeout=5)
        if ib.isConnected():
            print(f"SUCCESS: Connected with client ID {client_id}")
            ib.disconnect()
            sys.exit(0)
    except Exception as e:
        print(f"Failed with client ID {client_id}: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
print("No available client ID found in range 10-99.")
sys.exit(1)
