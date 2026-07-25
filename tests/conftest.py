"""Shared test setup.

Importing `app.main` constructs an `Anthropic()` client at module load, which
requires an API key. The tests never make a real call, so a dummy key is enough
to let the import succeed without a credential or network access. Set before any
test module imports `app.main`.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from sqlmodel import Session, SQLModel, create_engine  # noqa: E402
from sqlmodel.pool import StaticPool  # noqa: E402

# Where the suite runs. Unset (the default) means in-memory SQLite: fast, no
# service to start, and what a bare `pytest` gives you. Setting it to a Postgres
# URL runs the *same* tests against a real Postgres — CI does both.
#
# This matters because `/stats` and `/stats/timeseries` branch on the SQL
# dialect: ordered-set aggregates and epoch bucketing on Postgres, a Python
# fallback on SQLite. Running only SQLite would leave the production branch
# untested, which is the gap this closes.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

RUNNING_ON_POSTGRES = bool(TEST_DATABASE_URL)


def make_engine():
    """A fresh, empty schema for one test.

    On Postgres the schema is dropped and recreated, because every test shares
    one database and would otherwise see the previous test's rows. On SQLite each
    in-memory engine is already private, but needs StaticPool so a background
    thread (the sink) sees the same database rather than a new empty one.
    """
    if TEST_DATABASE_URL:
        engine = create_engine(TEST_DATABASE_URL)
        # Guardrail: this function DROPS every table. Pointing TEST_DATABASE_URL at
        # a real database would destroy it, so refuse anything not named as a test
        # database. Cheap insurance against one bad shell export.
        name = engine.url.database or ""
        if "test" not in name:
            raise RuntimeError(
                f"refusing to drop tables in database {name!r}: TEST_DATABASE_URL "
                "must name a test database (its name must contain 'test')"
            )
        SQLModel.metadata.drop_all(engine)
        SQLModel.metadata.create_all(engine)
        return engine

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def make_session() -> Session:
    """A session on a fresh schema, for tests that use a session directly."""
    return Session(make_engine())
