"""Auto-instrumentation: monkey-patch a provider client so a plain, un-wrapped
call is captured automatically — the zero-touch alternative to TracedClient.

Reuses everything (adapters, InferenceLog, sink, _preview→redaction); only the
application mechanism differs (patch vs wrap). The patched method OBSERVES only:
it returns the provider's raw response unchanged. See docs/auto-instrumentation.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from datetime import datetime, timezone
from typing import Callable

from .events import InferenceLog
from .providers import ADAPTERS
from .tracing import _preview  # single preview helper (truncate + PII redaction)

# Ambient session id (OTel-style context) so the call site stays pristine.
_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_session_id", default=None
)

# provider -> (resource attr, method name) to patch
_TARGETS = {
    "anthropic": ("messages", "create"),
    "gemini": ("models", "generate_content"),
}


@contextlib.contextmanager
def session_scope(session_id: str | None):
    """Tag every instrumented call inside this block with `session_id`."""
    token = _session.set(session_id)
    try:
        yield
    finally:
        _session.reset(token)


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
    def wrapper(*args, **kwargs):
        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        model = kwargs.get("model")
        input_preview = _preview(_last_user_preview(provider, kwargs))
        session_id = _session.get()

        try:
            response = original(*args, **kwargs)
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
                    ended_at=datetime.now(timezone.utc),
                    latency_ms=(time.perf_counter() - start) * 1000,
                    input_preview=input_preview,
                )
            )
            raise

        parsed = adapter.parse(response)
        sink(
            InferenceLog(
                session_id=session_id,
                provider=provider,
                model=parsed.model or model or "unknown",
                status="success",
                started_at=started_at,
                ended_at=datetime.now(timezone.utc),
                latency_ms=(time.perf_counter() - start) * 1000,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                input_preview=input_preview,
                output_preview=_preview(parsed.text),
            )
        )
        return response  # raw, unchanged — we only observe

    wrapper._auto_instrumented = True
    return wrapper


def instrument(client, provider: str, sink: Callable[[InferenceLog], None]):
    """Patch `client` so its generation method auto-emits an InferenceLog around
    each call. Idempotent — re-instrumenting the same client is a no-op. Returns
    the client for convenience."""
    adapter = ADAPTERS[provider]
    resource_attr, method_name = _TARGETS[provider]
    resource = getattr(client, resource_attr)
    original = getattr(resource, method_name)
    if getattr(original, "_auto_instrumented", False):
        return client
    setattr(resource, method_name, _make_wrapper(original, provider, adapter, sink))
    return client
