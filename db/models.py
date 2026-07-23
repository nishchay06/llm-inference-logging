import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(SQLModel, table=True):
    """One row per chat session. Owned by the chatbot service."""

    __tablename__ = "conversations"

    session_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Message(SQLModel, table=True):
    """One row per turn. Owned by the chatbot service."""

    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="conversations.session_id", index=True)
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=_now, index=True)


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
    started_at: datetime = Field(index=True)
    ended_at: datetime
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    created_at: datetime = Field(default_factory=_now)
