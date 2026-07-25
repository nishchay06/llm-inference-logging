import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InferenceLog(BaseModel):
    """One structured record per LLM call — the wire contract between the SDK and
    the ingestion service. Single source of truth for the metadata shape: the
    ingestion API validates incoming payloads against it and the worker parses
    them with it, so it is defined once, here.

    Note `status` is an open string rather than an enum: a streaming call can end
    "cancelled", and a telemetry schema should tolerate a producer that knows a
    status this consumer doesn't yet."""

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
    ttft_ms: float | None = None  # time to first token — streaming only

    # Usage (null on error, since no response came back)
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Content previews (truncated — we log a peek, not the whole payload)
    input_preview: str | None = None
    output_preview: str | None = None
