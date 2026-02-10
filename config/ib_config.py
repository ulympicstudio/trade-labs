"""IB connection configuration values loaded from environment."""
from dotenv import load_dotenv
import os


# Load from .env if present
load_dotenv()

# Defaults match common TWS paper defaults
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", 7497))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", 1))

__all__ = ["IB_HOST", "IB_PORT", "IB_CLIENT_ID"]
