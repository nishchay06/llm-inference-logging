import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def to_utc_naive(value: datetime) -> datetime:
    """Normalise a datetime to **naive UTC** — the convention every timestamp
    column here stores. Naive input is assumed to already be UTC."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class UtcDateTime(sa.types.TypeDecorator):
    """A DateTime column that always stores naive UTC, whatever it is handed.

    This is not cosmetic. The underlying column is `TIMESTAMP WITHOUT TIME ZONE`,
    and when Postgres is handed an *aware* datetime for such a column it converts
    to the session's `TimeZone` and only then discards the offset. The SDK stamps
    aware UTC timestamps, so against a server running in Asia/Kolkata every
    stored timestamp lands +5:30 off, while the identical code against a UTC
    server stores it correctly — the data silently depends on a server setting,
    and the dashboard's time buckets and `since` filters inherit the error.

    Normalising in `process_bind_param` makes the convention **structural**: it
    holds for every write path, including code that constructs a row directly
    rather than going through the ingestion boundary. SQLModel does not run
    Pydantic validators on `table=True` models, so a field validator would not
    have covered that case.

    (`TIMESTAMPTZ` would make the whole class of bug impossible and is the better
    end state — see the README's improvements. It is deferred here because it is a
    column-type change and there are no migrations yet, and because SQLite ignores
    `timezone=True`, which would reintroduce a backend divergence in the tests.)
    """

    impl = sa.types.DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else to_utc_naive(value)


def _now() -> datetime:
    return to_utc_naive(datetime.now(timezone.utc))


class Conversation(SQLModel, table=True):
    """One row per chat session. Owned by the chatbot service."""

    __tablename__ = "conversations"

    session_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)


class Message(SQLModel, table=True):
    """One row per turn. Owned by the chatbot service."""

    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="conversations.session_id", index=True)
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=_now, index=True, sa_type=UtcDateTime)


class InferenceLogRow(SQLModel, table=True):
    """One row per LLM call. Owned by the ingestion service.

    The storage model — deliberately separate from the wire model
    (`sdk.events.InferenceLog`) so the transport contract and the database
    schema can evolve independently.
    """

    __tablename__ = "inference_logs"

    event_id: str = Field(default_factory=_uuid, primary_key=True)
    # Soft reference, NOT a foreign key: telemetry is written by a different
    # service and may reference a session that has no conversation row (e.g. an
    # error before any message was stored). Indexed for lookups, but decoupled
    # from the app tables on purpose.
    session_id: str | None = Field(default=None, index=True)
    provider: str
    model: str
    status: str = Field(index=True)
    error_type: str | None = None
    error_message: str | None = None
    started_at: datetime = Field(index=True, sa_type=UtcDateTime)
    ended_at: datetime = Field(sa_type=UtcDateTime)
    latency_ms: float
    ttft_ms: float | None = None  # time to first token — streaming only
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)
