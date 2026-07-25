"""Tests for the TracedClient wrapper.

The key idea: we never call the real Claude API here. We pass in a FAKE client
(a stub that returns a canned response, or raises on demand) and a FAKE sink (a
plain list that records what it received). This is only possible because the
wrapper takes its client and sink as inputs — good design makes code testable.
"""

from types import SimpleNamespace

import pytest

from sdk.tracing import TracedClient


def make_response(text="hello", model="claude-sonnet-5", in_tok=5, out_tok=3):
    """A stand-in for an Anthropic response: just the attributes the wrapper reads."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)
    return SimpleNamespace(content=[block], model=model, usage=usage)


class FakeMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeClient:
    """Duck-types the Anthropic client: it just needs a `.messages.create(...)`."""

    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response, error)


def test_success_is_captured():
    events = []  # our fake sink: records every InferenceLog it receives
    client = FakeClient(response=make_response(text="hi there", in_tok=7, out_tok=2))
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    resp = traced.chat(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "hello"}],
        session_id="s1",
    )

    # It returns a normalized ChatResult; .text is provider-agnostic, .raw keeps
    # the underlying response for escape-hatch access.
    assert resp.text == "hi there"
    assert resp.raw.content[0].text == "hi there"

    # Exactly one log, with the right metadata.
    assert len(events) == 1
    log = events[0]
    assert log.status == "success"
    assert log.provider == "anthropic"
    assert log.model == "claude-sonnet-5"
    assert log.session_id == "s1"
    assert log.input_tokens == 7
    assert log.output_tokens == 2
    assert log.input_preview == "hello"
    assert log.output_preview == "hi there"
    assert log.latency_ms >= 0
    assert log.error_type is None


def test_previews_are_pii_redacted():
    """PII in the input/output must be scrubbed in the logged previews."""
    events = []
    client = FakeClient(response=make_response(text="saved card 4111 1111 1111 1111"))
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    traced.chat(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "my email is bob@example.com"}],
    )
    log = events[0]
    assert "[EMAIL]" in log.input_preview and "bob@example.com" not in log.input_preview
    assert "[CARD]" in log.output_preview and "4111" not in log.output_preview


def test_error_is_captured_and_reraised():
    events = []
    client = FakeClient(error=ValueError("boom"))
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    # The wrapper OBSERVES the failure but re-raises it — the caller still sees it.
    with pytest.raises(ValueError):
        traced.chat(
            model="claude-sonnet-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "hello"}],
            session_id="s2",
        )

    assert len(events) == 1
    log = events[0]
    assert log.status == "error"
    assert log.error_type == "ValueError"
    assert "boom" in log.error_message
    assert log.input_tokens is None
    assert log.output_tokens is None
    assert log.input_preview == "hello"


def test_chat_unaffected_when_log_delivery_fails():
    """The core guarantee, end to end: if the log sink's delivery fails, the
    chat must still succeed. With a QueueSink, delivery happens on a background
    thread and its failure is swallowed."""
    from sdk.sinks import QueueSink

    def exploding(event):
        raise RuntimeError("ingestion down")

    client = FakeClient(response=make_response(text="still works"))
    traced = TracedClient(client, provider="anthropic", sink=QueueSink(exploding))

    resp = traced.chat(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "hi"}],
    )
    # The chat returns normally even though logging will fail in the background.
    assert resp.text == "still works"


def test_gemini_provider_is_captured():
    """Same wrapper, different provider: selecting provider='gemini' routes
    through the Gemini adapter and produces a normalized log — the chat code and
    the wrapper's core logic are unchanged."""
    events = []

    gem_usage = SimpleNamespace(prompt_token_count=9, candidates_token_count=4)
    gem_response = SimpleNamespace(
        text="bonjour", usage_metadata=gem_usage, model_version="gemini-2.0-flash"
    )
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kw: gem_response)
    )

    traced = TracedClient(client, provider="gemini", sink=events.append)
    resp = traced.chat(
        model="gemini-2.0-flash",
        max_tokens=10,
        messages=[{"role": "user", "content": "hi"}],
        session_id="g1",
    )

    assert resp.text == "bonjour"
    assert len(events) == 1
    log = events[0]
    assert log.provider == "gemini"
    assert log.model == "gemini-2.0-flash"
    assert log.input_tokens == 9
    assert log.output_tokens == 4
    assert log.output_preview == "bonjour"
