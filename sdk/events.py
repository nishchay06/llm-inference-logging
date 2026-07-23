import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InferenceLog(BaseModel):
    """One structured record per LLM call. This is the single source of truth
    for the metadata shape — the ingestion API (Rung 4) validates against it and
    the database (Rung 6) stores it, so we define it once, here."""

    # Identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str | None = None

    # What ran
    provider: str
    model: str

    # Outcome
    status: str  # "success" | "error"
    error_type: str | None = None
    error_message: str | None = None

    # Timing
    started_at: datetime
    ended_at: datetime
    latency_ms: float

    # Usage (null on error, since no response came back)
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Content previews (truncated — we log a peek, not the whole payload)
    input_preview: str | None = None
    output_preview: str | None = None
