"""One-time schema setup: create tables if they don't exist.

Run once before starting the services:

    python -m db.init

Why this is a separate step and not done on app startup: having both services
race to CREATE TABLE on boot causes a concurrent-DDL error (Postgres catalog
unique violation). Schema management is a single-owner concern — real systems
use migrations (e.g. Alembic); this is the minimal stand-in.
"""

from db.engine import init_db

if __name__ == "__main__":
    init_db()
    print("schema created / verified")
