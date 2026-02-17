# config/ib_config.py
# Single source of truth for IB connection settings

HOST = "127.0.0.1"
PORT = 7497      # Paper TWS port
CLIENT_ID = 13   # Change if you see "client id already in use"

# Backward compatibility aliases (so either naming works)
IB_HOST = HOST
IB_PORT = PORT
IB_CLIENT_ID = CLIENT_ID
