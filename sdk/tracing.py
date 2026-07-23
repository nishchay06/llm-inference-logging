import time
from datetime import datetime, timezone
from typing import Any, Callable

from anthropic import Anthropic

from .events import InferenceLog

PREVIEW_CHARS = 200


def _preview(text: str) -> str:
    text = text.strip()
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


class TracedClient:
    """Wraps an Anthropic client and captures an InferenceLog around each call,
    handing it to a sink.

    `chat()` mirrors the raw SDK surface and returns the raw response, so callers
    treat it as a drop-in replacement for `client.messages.create`. That
    transparency is exactly what lets auto-instrumentation swap in later without
    the chat code changing.
    """

    def __init__(
        self,
        client: Anthropic,
        provider: str,
        sink: Callable[[InferenceLog], None],
    ):
        self._client = client
        self._provider = provider
        self._sink = sink  # injected, not hardcoded — swappable per rung

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        session_id: str | None = None,
        **kwargs: Any,
    ):
        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()

        # input preview = the latest user message we're about to send
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        input_preview = _preview(
            last_user if isinstance(last_user, str) else str(last_user)
        )

        try:
            response = self._client.messages.create(
                model=model, messages=messages, **kwargs
            )
        except Exception as exc:
            # Capture the failed inference, then re-raise: we only OBSERVE the
            # error here — the caller (FastAPI) still handles it.
            self._sink(
                InferenceLog(
                    session_id=session_id,
                    provider=self._provider,
                    model=model,
                    status="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc),
                    latency_ms=(time.perf_counter() - start) * 1000,
                    input_preview=input_preview,
                )
            )
            raise

        ended_at = datetime.now(timezone.utc)
        latency_ms = (time.perf_counter() - start) * 1000
        reply = next((b.text for b in response.content if b.type == "text"), "")

        self._sink(
            InferenceLog(
                session_id=session_id,
                provider=self._provider,
                model=response.model,
                status="success",
                started_at=started_at,
                ended_at=ended_at,
                latency_ms=latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                input_preview=input_preview,
                output_preview=_preview(reply),
            )
        )
        return response
