"""Capture core — the single implementation of *what* a traced LLM call records.

Both instrumentation mechanisms drive this module:

- `TracedClient` (`sdk/tracing.py`) wraps a client and calls in explicitly.
- `instrument()` (`sdk/instrument.py`) patches a client's methods to call in.

Only the *application* mechanism differs — wrap vs. patch. Timing, TTFT, status
semantics, redaction and log construction live here so they are defined once. That
matters most for streaming: "emit exactly one log at close, success/cancelled/error,
TTFT on the first delta" is subtle enough that two implementations would drift.

Nothing here imports a provider SDK. It deals in callables, deltas and parsed
results, which is why it can be tested directly (`tests/test_capture.py`).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from .events import InferenceLog
from .redaction import redact

PREVIEW_CHARS = 200


def preview(text: str) -> str:
    """Truncate for storage, but redact PII on the FULL text first — truncating
    before scrubbing could leave a half-matched value that no detector catches.
    Every preview in the system flows through here."""
    if not text:
        return text
    text = redact(text.strip())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def capture_call(
    call: Callable[[], Any],
    *,
    provider: str,
    model: str,
    sink: Callable[[InferenceLog], None],
    session_id: str | None,
    input_preview: str,
    parse: Callable[[Any], Any],
) -> tuple[Any, Any]:
    """Run a non-streaming call, emit exactly one log, and return
    `(raw_response, parsed_result)`.

    Returning both shapes is deliberate: the patch must hand the caller the
    provider's raw response untouched, while the wrapper wants the normalized
    result. Neither needs to re-derive what the other has.

    On failure the log is recorded and the exception **re-raised** — capture
    observes, it never suppresses. A caller's error handling must behave exactly
    as it would with no instrumentation present.
    """
    started_at = _utcnow()
    start = time.perf_counter()
    scrubbed_input = preview(input_preview)

    try:
        response = call()
    except Exception as exc:
        sink(
            InferenceLog(
                session_id=session_id,
                provider=provider,
                model=model or "unknown",
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                ended_at=_utcnow(),
                latency_ms=(time.perf_counter() - start) * 1000,
                input_preview=scrubbed_input,
            )
        )
        raise

    ended_at = _utcnow()
    latency_ms = (time.perf_counter() - start) * 1000
    parsed = parse(response)

    sink(
        InferenceLog(
            session_id=session_id,
            provider=provider,
            model=parsed.model or model or "unknown",
            status="success",
            started_at=started_at,
            ended_at=ended_at,
            latency_ms=latency_ms,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            input_preview=scrubbed_input,
            output_preview=preview(parsed.text),
        )
    )
    return response, parsed


class StreamCapture:
    """Accumulating state for one streamed call.

    A stream inverts the non-streaming problem: text arrives incrementally, but
    usage and total latency only at the end. So the capture is a small state
    machine — deltas are fed in as they pass the consumer, and exactly one log is
    emitted when the stream closes, however it closed.

    It is driven from two very different call shapes: a generator-based tee (the
    adapter's `stream()`, used by the wrapper) and a context-manager proxy (the
    provider SDK's own stream object, used by the patch). Both just call
    `on_delta` then one `emit_*`.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        sink: Callable[[InferenceLog], None],
        session_id: str | None,
        input_preview: str,
    ):
        self._provider = provider
        self._model = model
        self._sink = sink
        self._session_id = session_id
        self._input_preview = preview(input_preview)

        self.started_at = _utcnow()
        self._start = time.perf_counter()
        self.ttft_ms: float | None = None
        self.chunks: list[str] = []
        self._emitted = False

    def on_delta(self, text: str) -> None:
        """Record a delta on its way to the consumer. Stamps TTFT on the first."""
        if self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - self._start) * 1000
        if text:
            self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def _emit(self, **fields: Any) -> None:
        # At-most-once. Both drivers have two potential close paths (a generator's
        # finally plus a context manager's __exit__), and a double emit would
        # double-count every stream in the dashboard.
        if self._emitted:
            return
        self._emitted = True
        self._sink(
            InferenceLog(
                session_id=self._session_id,
                provider=self._provider,
                started_at=self.started_at,
                ended_at=_utcnow(),
                latency_ms=(time.perf_counter() - self._start) * 1000,
                ttft_ms=self.ttft_ms,
                input_preview=self._input_preview,
                **fields,
            )
        )

    def emit_success(
        self,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        text: str | None = None,
    ) -> None:
        self._emit(
            model=model or self._model or "unknown",
            status="success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Redaction runs on the JOINED text: a value split across two deltas
            # would slip past a per-delta scrub.
            output_preview=preview(text if text is not None else self.text),
        )

    def emit_cancelled(self) -> None:
        """The consumer stopped reading — a client disconnect or an explicit
        cancel. Whatever was produced is kept; usage is unknown because the
        provider never got to report it."""
        self._emit(
            model=self._model or "unknown",
            status="cancelled",
            output_preview=preview(self.text),
        )

    def emit_error(self, exc: BaseException) -> None:
        self._emit(
            model=self._model or "unknown",
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
            output_preview=preview(self.text),
        )


def tee_stream(
    gen,
    *,
    provider: str,
    model: str,
    sink: Callable[[InferenceLog], None],
    session_id: str | None,
    input_preview: str,
):
    """Tee an adapter-shaped stream: a generator that yields text deltas and
    `return`s a `ChatResult`.

    Yields each delta straight through to the consumer while accumulating, then
    emits one log. The three ways a stream can end each get their own status —
    normal completion, `GeneratorExit` (consumer closed it: a cancel), or an
    exception from the provider.
    """
    capture = StreamCapture(
        provider=provider,
        model=model,
        sink=sink,
        session_id=session_id,
        input_preview=input_preview,
    )
    parsed = None
    try:
        while True:
            try:
                delta = next(gen)
            except StopIteration as stop:
                parsed = stop.value  # adapters return their ChatResult here
                break
            capture.on_delta(delta)
            yield delta

        capture.emit_success(
            model=(parsed.model if parsed else None),
            input_tokens=(parsed.input_tokens if parsed else None),
            output_tokens=(parsed.output_tokens if parsed else None),
            text=((parsed.text if parsed else "") or capture.text),
        )
    except GeneratorExit:
        capture.emit_cancelled()
        raise
    except Exception as exc:
        capture.emit_error(exc)
        raise
    finally:
        gen.close()
