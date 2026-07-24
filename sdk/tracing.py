from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from .events import InferenceLog
from .providers import ADAPTERS, ChatResult

PREVIEW_CHARS = 200


def _preview(text: str) -> str:
    text = text.strip()
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


class TracedClient:
    """Wraps a provider client and captures an InferenceLog around each call,
    handing it to a sink.

    Provider-specific behaviour (how to call, how to read the response) lives in
    a per-provider adapter, resolved from the `provider` name. This wrapper only
    does the provider-agnostic work: timing, error capture, building the log, and
    emitting it. `chat()` returns a normalized `ChatResult`, so the chat code
    never sees a provider-specific response shape.
    """

    def __init__(
        self,
        client: Any,
        provider: str,
        sink: Callable[[InferenceLog], None],
    ):
        self._client = client
        self._provider = provider
        self._adapter = ADAPTERS[provider]
        self._sink = sink  # injected, not hardcoded — swappable per rung

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResult:
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
            response = self._adapter.create(
                self._client,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
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
        parsed = self._adapter.parse(response)

        self._sink(
            InferenceLog(
                session_id=session_id,
                provider=self._provider,
                model=parsed.model or model,
                status="success",
                started_at=started_at,
                ended_at=ended_at,
                latency_ms=latency_ms,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                input_preview=input_preview,
                output_preview=_preview(parsed.text),
            )
        )
        return parsed
