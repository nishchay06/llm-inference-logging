from __future__ import annotations

from typing import Any, Callable

from .capture import capture_call, tee_stream
from .events import InferenceLog
from .providers import ADAPTERS, ChatResult


def _last_user_text(messages: list[dict]) -> str:
    """The message we're about to send, for the input preview."""
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return last if isinstance(last, str) else str(last)


class TracedClient:
    """Wraps a provider client and captures an InferenceLog around each call,
    handing it to a sink.

    This is the **explicit** instrumentation mechanism — the alternative to
    patching (`sdk/instrument.py`), which the app uses by default. Both share the
    same capture core (`sdk/capture.py`), so they record identical fields; only
    the way capture is applied differs. Keeping this class means there is a
    non-magical path that is easy to reason about, and a one-variable rollback.

    Provider-specific behaviour (how to call, how to read the response) lives in a
    per-provider adapter, resolved from the `provider` name. This wrapper only
    does the provider-agnostic work: delegating to capture and returning a
    normalized `ChatResult`, so chat code never sees a provider-specific shape.
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
        self._sink = sink  # injected, not hardcoded — the destination is swappable

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        _, parsed = capture_call(
            lambda: self._adapter.create(
                self._client,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
            ),
            provider=self._provider,
            model=model,
            sink=self._sink,
            session_id=session_id,
            input_preview=_last_user_text(messages),
            parse=self._adapter.parse,
        )
        return parsed

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        session_id: str | None = None,
        **kwargs: Any,
    ):
        """Streaming twin of `chat()`. Tees the provider's text deltas to the
        caller while accumulating them, and emits exactly one InferenceLog when
        the stream ends — success on completion, `cancelled` if the consumer
        closes the generator (client disconnect), `error` on failure. TTFT is
        recorded on the first delta."""
        return tee_stream(
            self._adapter.stream(
                self._client,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
            ),
            provider=self._provider,
            model=model,
            sink=self._sink,
            session_id=session_id,
            input_preview=_last_user_text(messages),
        )
