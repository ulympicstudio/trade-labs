# config/ib_config.py
# Single source of truth for IB connection settings

import os

HOST = "127.0.0.1"
PORT = 7497      # Paper TWS port
CLIENT_ID = int(os.getenv("IB_CLIENT_ID_OVERRIDE", "13"))   # Override with env var if needed

# Backward compatibility aliases (so either naming works)
IB_HOST = HOST
IB_PORT = PORT
IB_CLIENT_ID = CLIENT_ID
