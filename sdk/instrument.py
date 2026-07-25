"""Auto-instrumentation: monkey-patch a provider client so a plain, un-wrapped
call is captured automatically — the zero-touch alternative to TracedClient.

Reuses everything (adapters, the capture core, sink, redaction); only the
application mechanism differs (patch vs wrap). The patched method OBSERVES only:
it returns the provider's raw response unchanged. See docs/auto-instrumentation.md.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Callable

from .capture import StreamCapture, capture_call, preview  # shared capture core
from .events import InferenceLog
from .providers import ADAPTERS

# Ambient session id (OTel-style context) so the call site stays pristine.
_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_session_id", default=None
)

# provider -> [(resource attr, method name, shape)] to patch.
#
# Both call shapes must be covered. Patching only the non-streaming method would
# mean a streaming app silently produces no telemetry at all — no TTFT, no
# cancelled status, nothing. The two providers stream differently, hence `shape`:
#
#   "call"        plain method, returns a response
#   "stream_cm"   returns a CONTEXT MANAGER whose text arrives via `.text_stream`
#                 and whose usage arrives via `.get_final_message()` (Anthropic)
#   "stream_iter" returns a plain ITERATOR of chunks, usage on the last ones (Gemini)
_TARGETS = {
    "anthropic": [
        ("messages", "create", "call"),
        ("messages", "stream", "stream_cm"),
    ],
    "gemini": [
        ("models", "generate_content", "call"),
        ("models", "generate_content_stream", "stream_iter"),
    ],
}


@contextlib.contextmanager
def session_scope(session_id: str | None):
    """Tag every instrumented call inside this block with `session_id`.

    Restores the previous *value* rather than resetting a Token. A Token may only
    be reset in the context that created it, and this scope can legitimately span
    a generator whose steps run in different contexts — FastAPI serves a sync
    streaming response through a threadpool, so entry and exit land on different
    threads. Token-based reset raised `was created in a different Context` there,
    which surfaced as a spurious error at the end of an otherwise perfect stream.
    Set-by-value has no such constraint and is equivalent here, since the variable
    defaults to None rather than being meaningfully "unset".
    """
    previous = _session.get()
    _session.set(session_id)
    try:
        yield
    finally:
        _session.set(previous)


def _last_user_preview(provider: str, kwargs: dict) -> str:
    """Best-effort input preview from the native call kwargs (provider-aware)."""
    if provider == "gemini":
        for content in reversed(kwargs.get("contents") or []):
            if isinstance(content, dict) and content.get("role") in (None, "user"):
                parts = content.get("parts") or []
                if parts and isinstance(parts[0], dict):
                    return parts[0].get("text", "")
        return ""
    messages = kwargs.get("messages") or []
    return next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )


def _make_wrapper(original: Callable, provider: str, adapter, sink: Callable):
    """Patch for a plain (non-streaming) generation method."""

    def wrapper(*args, **kwargs):
        response, _ = capture_call(
            lambda: original(*args, **kwargs),
            provider=provider,
            model=kwargs.get("model") or "",
            sink=sink,
            session_id=_session.get(),
            input_preview=_last_user_preview(provider, kwargs),
            parse=adapter.parse,
        )
        return response  # raw, unchanged — we only observe

    wrapper._auto_instrumented = True
    return wrapper


def _new_capture(provider, adapter, sink, kwargs) -> StreamCapture:
    return StreamCapture(
        provider=provider,
        model=kwargs.get("model") or "",
        sink=sink,
        # Read the ambient session id NOW, at call time. A stream's log is emitted
        # when it closes, by which point the surrounding session_scope may already
        # have exited — so it cannot be read then.
        session_id=_session.get(),
        input_preview=_last_user_preview(provider, kwargs),
    )


class _TracedStream:
    """Proxy for the SDK's stream object that tees `text_stream`.

    A proxy rather than mutating the SDK's object: `text_stream` is an instance
    attribute on Anthropic's `MessageStream` today, but proxying works whether it
    is an attribute or a property, and never writes to a third-party object.
    Everything else delegates untouched.
    """

    def __init__(self, inner, capture: StreamCapture):
        self._inner = inner
        self._capture = capture

    def __getattr__(self, name):  # only reached for names not set on the proxy
        return getattr(self._inner, name)

    @property
    def text_stream(self):
        for delta in self._inner.text_stream:
            self._capture.on_delta(delta)
            yield delta

    def __iter__(self):
        return iter(self._inner)


class _TracedStreamManager:
    """Proxy for a stream context manager (Anthropic's `messages.stream`).

    The log is emitted from `__exit__`, which is the one place that sees every way
    a stream can end: normally, via `GeneratorExit` when the consumer abandons it
    (a client disconnect), or via a provider exception.
    """

    def __init__(self, manager, capture: StreamCapture, adapter):
        self._manager = manager
        self._capture = capture
        self._adapter = adapter
        self._stream = None

    def __enter__(self):
        self._stream = self._manager.__enter__()
        return _TracedStream(self._stream, self._capture)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._emit_success()
        elif issubclass(exc_type, GeneratorExit):
            self._capture.emit_cancelled()
        else:
            self._capture.emit_error(exc if exc is not None else exc_type())
        return self._manager.__exit__(exc_type, exc, tb)

    def _emit_success(self):
        model = input_tokens = output_tokens = None
        try:
            model, input_tokens, output_tokens = self._adapter.stream_usage(
                self._stream.get_final_message()
            )
        except Exception:
            # Usage is only available once the stream is fully consumed. A caller
            # that exited early still gets a log — just without token counts.
            pass
        self._capture.emit_success(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens
        )


def _make_stream_cm_wrapper(original: Callable, provider: str, adapter, sink: Callable):
    def wrapper(*args, **kwargs):
        capture = _new_capture(provider, adapter, sink, kwargs)
        try:
            manager = original(*args, **kwargs)
        except Exception as exc:
            capture.emit_error(exc)  # failed before a stream even opened
            raise
        return _TracedStreamManager(manager, capture, adapter)

    wrapper._auto_instrumented = True
    return wrapper


def _make_stream_iter_wrapper(original: Callable, provider: str, adapter, sink: Callable):
    def wrapper(*args, **kwargs):
        capture = _new_capture(provider, adapter, sink, kwargs)
        try:
            inner = original(*args, **kwargs)
        except Exception as exc:
            capture.emit_error(exc)
            raise
        return _traced_chunks(inner, capture, adapter)

    wrapper._auto_instrumented = True
    return wrapper


def _traced_chunks(inner, capture: StreamCapture, adapter):
    """Tee a chunk iterator, yielding the provider's chunks unchanged."""
    model = input_tokens = output_tokens = None
    try:
        for chunk in inner:
            text = adapter.delta_text(chunk)
            if text:
                capture.on_delta(text)
            # Usage arrives on the final chunk(s); keep the last non-null values.
            m, i, o = adapter.stream_usage(chunk)
            model = m or model
            input_tokens = i if i is not None else input_tokens
            output_tokens = o if o is not None else output_tokens
            yield chunk
        capture.emit_success(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens
        )
    except GeneratorExit:
        capture.emit_cancelled()
        raise
    except Exception as exc:
        capture.emit_error(exc)
        raise


_WRAPPERS = {
    "call": _make_wrapper,
    "stream_cm": _make_stream_cm_wrapper,
    "stream_iter": _make_stream_iter_wrapper,
}


def instrument(client, provider: str, sink: Callable[[InferenceLog], None]):
    """Patch `client` so its generation methods — streaming and non-streaming —
    auto-emit an InferenceLog around each call.

    Idempotent: re-instrumenting the same client is a no-op, per method. Raises
    `AttributeError` if a target method is missing, which is deliberate — an SDK
    that moved a method should fail loudly here rather than leave the app running
    with instrumentation that silently captures nothing. Returns the client.
    """
    adapter = ADAPTERS[provider]
    for resource_attr, method_name, shape in _TARGETS[provider]:
        resource = getattr(client, resource_attr)
        original = getattr(resource, method_name)  # AttributeError = loud failure
        if getattr(original, "_auto_instrumented", False):
            continue
        setattr(
            resource,
            method_name,
            _WRAPPERS[shape](original, provider, adapter, sink),
        )
    return client
