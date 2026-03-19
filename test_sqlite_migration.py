"""
Integration test: SQLite DB + migrations (repeatable every run).

Fixes:
- Deletes the old test DB file each run
- Uses a unique run_id
- Uses correct record_run() signature:
  (run_id, backend, armed, num_scanned, num_executed, num_successful, details)
"""

import os
import uuid

from src.database.db_manager import TradeLabsDB


def test_sqlite_migration():
    print("=" * 70)
    print("PHASE 3: SQLite Database & Migration Test")
    print("=" * 70)
    print()

    db_path = "data/test_trade_labs.db"
    os.makedirs("data", exist_ok=True)

    # Always start fresh so this test is repeatable
    if os.path.exists(db_path):
        os.remove(db_path)

    print("1. Initializing SQLite database...")
    db = TradeLabsDB(db_path=db_path)
    print(f"✓ Database created/verified: {db_path}")
    print()

    print("2. Testing basic database operations...")

    run_id = f"test_run_{uuid.uuid4().hex[:8]}"

    run = db.record_run(
        run_id=run_id,
        backend="SIM",
        armed=False,
        num_scanned=10,
        num_executed=5,
        num_successful=3,
        details={"test": True},
    )

    # record_run returns a dict
    print(f"✓ Recorded run: {run.get('run_id', run_id)}")
    print("\n✓ SQLite migration test completed successfully.")