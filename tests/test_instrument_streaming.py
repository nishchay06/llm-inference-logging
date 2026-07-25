"""Streaming auto-instrumentation — the patch layer's streaming half.

This is the gap that had to close before patching could become the app's default:
without it, `/chat/stream` would have produced no telemetry at all. The assertions
here deliberately mirror the wrapper's streaming tests (`test_streaming.py`) so
parity between the two mechanisms is literal rather than claimed.

The two providers expose different streaming shapes, and both are patched:

- Anthropic: `messages.stream(...)` returns a **context manager**; the text comes
  from `stream.text_stream` and usage from `stream.get_final_message()`.
- Gemini: `generate_content_stream(...)` returns a **plain iterator** of chunks,
  with usage arriving on the final chunk(s).

Fakes only — no network.
"""

from types import SimpleNamespace

import pytest

from sdk.instrument import instrument, session_scope


# ── fakes: Anthropic's context-manager shape ─────────────────────────────────

class _FakeMessageStream:
    """Stands in for the SDK's MessageStream. `text_stream` is an instance
    attribute here, matching the real SDK (it is assigned in __init__ from
    __stream_text__), not a class property."""

    def __init__(self, deltas, final, error=None):
        self._error = error
        self.text_stream = self._gen(deltas)
        self._final = final
        self.closed = False

    def _gen(self, deltas):
        for d in deltas:
            if self._error is not None and d == "BOOM":
                raise self._error
            yield d

    def get_final_message(self):
        return self._final

    def close(self):
        self.closed = True


class _FakeStreamManager:
    def __init__(self, deltas, final, error=None):
        self.stream = _FakeMessageStream(deltas, final, error)
        self.exited_with = None

    def __enter__(self):
        return self.stream

    def __exit__(self, exc_type, exc, tb):
        self.exited_with = exc_type
        return False


def _final_message(model="claude-sonnet-5", in_tok=5, out_tok=3):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def _fake_anthropic(deltas=("Hel", "lo"), final=None, error=None, call_error=None):
    final = final or _final_message()
    holder = {}

    def stream(**kwargs):
        if call_error is not None:
            raise call_error
        holder["manager"] = _FakeStreamManager(list(deltas), final, error)
        holder["kwargs"] = kwargs
        return holder["manager"]

    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: None, stream=stream)
    )
    return client, holder


# ── fakes: Gemini's iterator shape ───────────────────────────────────────────

def _gemini_chunk(text=None, model=None, prompt=None, cand=None):
    usage = None
    if prompt is not None or cand is not None:
        usage = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand)
    return SimpleNamespace(text=text, model_version=model, usage_metadata=usage)


def _fake_gemini(chunks=None, error=None):
    if chunks is None:
        chunks = [
            _gemini_chunk(text="Bon"),
            _gemini_chunk(text="jour", model="gemini-3.6-flash", prompt=9, cand=4),
        ]

    def generate_content_stream(**kwargs):
        def gen():
            for c in chunks:
                if error is not None and getattr(c, "text", None) == "BOOM":
                    raise error
                yield c

        return gen()

    return SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kw: None,
            generate_content_stream=generate_content_stream,
        )
    )


def _messages(text="hello"):
    return [{"role": "user", "content": text}]


def _consume_anthropic(client, **over):
    """Consume a patched Anthropic stream the way the adapter does."""
    kwargs = dict(model="claude-sonnet-5", messages=_messages(), max_tokens=16)
    kwargs.update(over)
    out = []
    with client.messages.stream(**kwargs) as stream:
        for delta in stream.text_stream:
            out.append(delta)
        stream.get_final_message()
    return out


# ── Anthropic: capture + transparency ────────────────────────────────────────

def test_anthropic_stream_emits_one_log_and_passes_deltas_through():
    events = []
    client, _ = _fake_anthropic(deltas=("Hel", "lo"))
    instrument(client, "anthropic", events.append)

    assert _consume_anthropic(client) == ["Hel", "lo"]  # observe-only

    (log,) = events
    assert log.status == "success"
    assert log.provider == "anthropic"
    assert log.output_preview == "Hello"
    assert log.input_preview == "hello"


def test_anthropic_stream_captures_usage_and_served_model():
    events = []
    client, _ = _fake_anthropic(final=_final_message(model="claude-sonnet-5-20260101", in_tok=11, out_tok=4))
    instrument(client, "anthropic", events.append)
    _consume_anthropic(client)

    (log,) = events
    assert log.model == "claude-sonnet-5-20260101"  # what actually served
    assert log.input_tokens == 11 and log.output_tokens == 4


def test_anthropic_stream_records_ttft_not_exceeding_total_latency():
    events = []
    client, _ = _fake_anthropic()
    instrument(client, "anthropic", events.append)
    _consume_anthropic(client)

    (log,) = events
    assert log.ttft_ms is not None
    assert log.ttft_ms <= log.latency_ms


def test_anthropic_stream_cancelled_keeps_partial_output():
    """The consumer abandons the stream mid-flight — a client disconnect. This is
    the behaviour that would have been lost entirely by switching the default
    before the patch layer handled streams."""
    events = []
    client, _ = _fake_anthropic(deltas=("Hel", "lo", " there"))
    instrument(client, "anthropic", events.append)

    def consume():
        with client.messages.stream(model="m", messages=_messages(), max_tokens=16) as stream:
            for delta in stream.text_stream:
                yield delta

    gen = consume()
    assert next(gen) == "Hel"
    gen.close()  # disconnect

    (log,) = events
    assert log.status == "cancelled"
    assert log.output_preview == "Hel"
    assert log.ttft_ms is not None


def test_anthropic_stream_error_mid_stream_is_recorded_and_reraised():
    events = []
    client, _ = _fake_anthropic(deltas=("Hel", "BOOM"), error=RuntimeError("stream died"))
    instrument(client, "anthropic", events.append)

    with pytest.raises(RuntimeError, match="stream died"):
        _consume_anthropic(client)

    (log,) = events
    assert log.status == "error"
    assert log.error_type == "RuntimeError"
    assert log.output_preview == "Hel"  # what got through is kept


def test_anthropic_stream_error_opening_the_stream_is_recorded():
    events = []
    client, _ = _fake_anthropic(call_error=ValueError("bad request"))
    instrument(client, "anthropic", events.append)

    with pytest.raises(ValueError, match="bad request"):
        client.messages.stream(model="m", messages=_messages(), max_tokens=16)

    (log,) = events
    assert log.status == "error"
    assert log.error_type == "ValueError"


def test_anthropic_stream_underlying_context_manager_still_exits():
    """We proxy the SDK's stream object; the real one must still be closed."""
    events = []
    client, holder = _fake_anthropic()
    instrument(client, "anthropic", events.append)
    _consume_anthropic(client)

    assert holder["manager"].exited_with is None  # exited, and cleanly


def test_anthropic_stream_proxy_delegates_unknown_attributes():
    """The proxy must not hide the rest of the SDK's stream surface."""
    events = []
    client, holder = _fake_anthropic()
    instrument(client, "anthropic", events.append)

    with client.messages.stream(model="m", messages=_messages(), max_tokens=16) as stream:
        list(stream.text_stream)
        stream.close()  # a method only the underlying object defines

    assert holder["manager"].stream.closed is True


# ── Gemini: the iterator shape ───────────────────────────────────────────────

def test_gemini_stream_emits_one_log_and_yields_raw_chunks():
    events = []
    client = _fake_gemini()
    instrument(client, "gemini", events.append)

    chunks = list(
        client.models.generate_content_stream(
            model="gemini-flash-latest", contents=[], config=None
        )
    )
    # Raw provider chunks, unchanged — the patch observes only.
    assert [c.text for c in chunks] == ["Bon", "jour"]

    (log,) = events
    assert log.status == "success"
    assert log.provider == "gemini"
    assert log.output_preview == "Bonjour"
    assert log.model == "gemini-3.6-flash"
    assert log.input_tokens == 9 and log.output_tokens == 4
    assert log.ttft_ms is not None


def test_gemini_stream_cancelled_keeps_partial_output():
    events = []
    client = _fake_gemini()
    instrument(client, "gemini", events.append)

    gen = client.models.generate_content_stream(model="m", contents=[], config=None)
    next(gen)
    gen.close()

    (log,) = events
    assert log.status == "cancelled"
    assert log.output_preview == "Bon"


def test_gemini_stream_error_is_recorded_and_reraised():
    events = []
    client = _fake_gemini(
        chunks=[_gemini_chunk(text="Bon"), _gemini_chunk(text="BOOM")],
        error=RuntimeError("gemini died"),
    )
    instrument(client, "gemini", events.append)

    with pytest.raises(RuntimeError, match="gemini died"):
        list(client.models.generate_content_stream(model="m", contents=[], config=None))

    (log,) = events
    assert log.status == "error"
    assert log.output_preview == "Bon"


# ── cross-cutting ────────────────────────────────────────────────────────────

def test_session_scope_threads_session_id_into_a_stream_log():
    events = []
    client, _ = _fake_anthropic()
    instrument(client, "anthropic", events.append)

    with session_scope("sess-42"):
        _consume_anthropic(client)

    assert events[0].session_id == "sess-42"


def test_stream_previews_are_pii_redacted():
    events = []
    client, _ = _fake_anthropic(deltas=("mail bob@", "example.com"))
    instrument(client, "anthropic", events.append)

    _consume_anthropic(client, messages=_messages("my card is 4111 1111 1111 1111"))

    (log,) = events
    assert "[CARD]" in log.input_preview and "4111" not in log.input_preview
    # The address spans two deltas: redaction must run on the joined text.
    assert "[EMAIL]" in log.output_preview
    assert "bob@example.com" not in log.output_preview


def test_instrumenting_streaming_twice_is_a_no_op():
    events = []
    client, _ = _fake_anthropic()
    instrument(client, "anthropic", events.append)
    instrument(client, "anthropic", events.append)

    _consume_anthropic(client)
    assert len(events) == 1  # not double-logged


def test_exactly_one_log_per_stream_even_though_close_paths_overlap():
    """A generator's finally and the context manager's __exit__ can both fire."""
    events = []
    client, _ = _fake_anthropic()
    instrument(client, "anthropic", events.append)

    with client.messages.stream(model="m", messages=_messages(), max_tokens=16) as stream:
        gen = stream.text_stream
        list(gen)
        gen.close()
        stream.get_final_message()

    assert len(events) == 1


# ── the real SDK surfaces (no network: we only touch attributes) ─────────────

def test_patches_the_real_anthropic_streaming_surface():
    """If the SDK moves or renames `messages.stream`, this fails loudly in CI
    rather than the app silently losing streaming telemetry."""
    from anthropic import Anthropic

    client = Anthropic(api_key="test-not-used")
    original = client.messages.stream
    instrument(client, "anthropic", lambda e: None)
    assert client.messages.stream is not original
    assert getattr(client.messages.stream, "_auto_instrumented", False)


def test_patches_the_real_gemini_streaming_surface():
    from google import genai

    client = genai.Client(api_key="test-not-used")
    original = client.models.generate_content_stream
    instrument(client, "gemini", lambda e: None)
    assert client.models.generate_content_stream is not original
    assert getattr(client.models.generate_content_stream, "_auto_instrumented", False)
