"""The client used when instrumentation comes from **patching** the provider SDK.

`ProviderClient` does one job: call a provider through its adapter and return a
normalized `ChatResult`, so chat code never sees a provider-specific response
shape. It contains **no capture logic at all** — telemetry appears because the
underlying client was patched by `instrument()`.

That split is the point. `TracedClient` (`sdk/tracing.py`) does capture *and*
normalization, which is convenient but means the call site is choosing to be
instrumented. Here the two concerns are separate: capture is ambient and invisible,
exactly as an auto-instrumented system should be, and this class is what is left
over once capture is removed from the call path.

Both classes expose the same `chat()` / `stream()` surface, so they are drop-in
interchangeable and `app/main.py` reads the same whichever is configured.
"""

from __future__ import annotations

from typing import Any, Iterator

from .instrument import session_scope
from .providers import ADAPTERS, ChatResult


class ProviderClient:
    """Adapter-backed, normalizing, uninstrumented."""

    def __init__(self, client: Any, provider: str):
        self._client = client
        self._provider = provider
        self._adapter = ADAPTERS[provider]

    @property
    def raw(self) -> Any:
        """The underlying provider client (patched, if instrument() was applied)."""
        return self._client

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # session_scope publishes the id ambiently; the patched method reads it.
        # Note what is absent: no timing, no try/except, no sink.
        with session_scope(session_id):
            response = self._adapter.create(
                self._client,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
            )
        return self._adapter.parse(response)

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Yield text deltas. The patch tees them and emits one log at close.

        The scope has to span the whole generator, not just its construction: the
        adapter is itself a generator, so the patched streaming method is not
        called until the first `next()`. Generators do not isolate context, so the
        id is visible to this thread while the stream is open — and reliably reset
        when the generator finishes or is closed, which the tests pin.
        """
        with session_scope(session_id):
            yield from self._adapter.stream(
                self._client,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
            )
