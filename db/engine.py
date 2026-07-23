import getpass
import os

from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (import registers the tables on SQLModel.metadata)

# SQLAlchemy URL using the psycopg (v3) driver. Local default works out of the
# box with a Postgres server listening on :5432 and a `chatbot` database owned
# by the current OS user; override with DATABASE_URL for other environments
# (e.g. Docker).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql+psycopg://{getpass.getuser()}@localhost:5432/chatbot",
)

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    """Create any missing tables. Idempotent; safe to call on every startup.
    (A real system would use Alembic migrations — see README.)"""
    SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI dependency: yields a session, closed after the request.
    Overridable in tests to point at an in-memory SQLite engine."""
    with Session(engine) as session:
        yield session
