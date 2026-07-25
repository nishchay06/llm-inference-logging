"""Tests for ProviderClient (sdk/client.py) — the client the app uses when
instrumentation comes from patching rather than wrapping.

Its defining property is what it does *not* do: it normalizes provider responses
into a `ChatResult` and carries no capture logic at all. Telemetry arrives because
the underlying client is patched. The first two tests pin exactly that, since it
is the architectural claim the default rests on: given an un-patched client,
ProviderClient emits nothing; given a patched one, exactly one log per call.

It exposes the same `chat()` / `stream()` surface as TracedClient so the two are
drop-in interchangeable and `app/main.py` reads identically either way.
"""

from types import SimpleNamespace

import pytest

from sdk.client import ProviderClient
from sdk.instrument import instrument


# ── fakes ────────────────────────────────────────────────────────────────────

def _response(text="hi there", model="claude-sonnet-5", in_tok=7, out_tok=2):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class _FakeStream:
    def __init__(self, deltas, final):
        self.text_stream = iter(deltas)
        self._final = final

    def get_final_message(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_anthropic(text="hi there", deltas=("Hel", "lo")):
    final = SimpleNamespace(
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
    )
    return SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _response(text=text),
            stream=lambda **kw: _FakeStream(list(deltas), final),
        )
    )


def _fake_gemini(text="bonjour"):
    resp = SimpleNamespace(
        text=text,
        model_version="gemini-3.6-flash",
        usage_metadata=SimpleNamespace(prompt_token_count=9, candidates_token_count=4),
    )

    def _stream(**kw):
        yield SimpleNamespace(text="bon", model_version=None, usage_metadata=None)
        yield SimpleNamespace(
            text="jour",
            model_version="gemini-3.6-flash",
            usage_metadata=SimpleNamespace(
                prompt_token_count=9, candidates_token_count=4
            ),
        )

    return SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kw: resp, generate_content_stream=_stream
        )
    )


MESSAGES = [{"role": "user", "content": "hello"}]


# ── the architectural claim: no capture of its own ───────────────────────────

def test_emits_nothing_when_the_client_is_not_patched():
    """ProviderClient must contain zero instrumentation. If this ever fails, the
    app is double-counting: capture would exist both here and in the patch."""
    events = []
    client = ProviderClient(_fake_anthropic(), "anthropic")

    result = client.chat(model="claude-sonnet-5", messages=MESSAGES, session_id="s1")
    list(client.stream(model="claude-sonnet-5", messages=MESSAGES, session_id="s1"))

    assert result.text == "hi there"  # still normalizes
    assert events == []  # but records nothing


def test_emits_exactly_one_log_per_call_when_the_client_is_patched():
    events = []
    raw = instrument(_fake_anthropic(), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")

    client.chat(model="claude-sonnet-5", messages=MESSAGES, session_id="s1")
    assert len(events) == 1

    list(client.stream(model="claude-sonnet-5", messages=MESSAGES, session_id="s1"))
    assert len(events) == 2


# ── normalization ────────────────────────────────────────────────────────────

def test_chat_returns_a_normalized_result_for_anthropic():
    client = ProviderClient(_fake_anthropic(text="hello world"), "anthropic")
    result = client.chat(model="claude-sonnet-5", messages=MESSAGES)

    assert result.text == "hello world"
    assert result.model == "claude-sonnet-5"
    assert result.input_tokens == 7 and result.output_tokens == 2
    assert result.raw is not None  # escape hatch preserved


def test_chat_returns_a_normalized_result_for_gemini():
    """Same shape from a provider whose response looks nothing like Anthropic's."""
    client = ProviderClient(_fake_gemini(text="bonjour"), "gemini")
    result = client.chat(model="gemini-flash-latest", messages=MESSAGES)

    assert result.text == "bonjour"
    assert result.model == "gemini-3.6-flash"
    assert result.input_tokens == 9 and result.output_tokens == 4


def test_stream_yields_text_deltas_for_both_providers():
    anthropic = ProviderClient(_fake_anthropic(deltas=("Hel", "lo")), "anthropic")
    assert list(anthropic.stream(model="m", messages=MESSAGES)) == ["Hel", "lo"]

    gemini = ProviderClient(_fake_gemini(), "gemini")
    assert list(gemini.stream(model="m", messages=MESSAGES)) == ["bon", "jour"]


# ── session id threading (the ambient contract) ──────────────────────────────

def test_session_id_reaches_the_log_without_being_passed_to_the_provider():
    """The call site names a session; the patch picks it up ambiently. Nothing
    provider-facing ever sees it."""
    events = []
    raw = instrument(_fake_anthropic(), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")

    client.chat(model="m", messages=MESSAGES, session_id="sess-7")
    assert events[0].session_id == "sess-7"


def test_session_id_reaches_a_streamed_log():
    events = []
    raw = instrument(_fake_anthropic(), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")

    list(client.stream(model="m", messages=MESSAGES, session_id="sess-8"))
    assert events[0].session_id == "sess-8"
    assert events[0].ttft_ms is not None


def test_ambient_session_does_not_leak_after_a_stream_finishes():
    """The scope must close even though it spans a generator's lifetime."""
    from sdk.instrument import _session

    events = []
    raw = instrument(_fake_anthropic(), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")

    list(client.stream(model="m", messages=MESSAGES, session_id="sess-9"))
    assert _session.get() is None


def test_stream_can_be_consumed_across_threads():
    """Regression: FastAPI serves a sync generator through a threadpool, so
    successive `next()` calls can run in *different* contexts. A contextvars
    Token may only be reset in the context that created it, so closing the scope
    on a later step raised `Token ... was created in a different Context`.

    The stream and its log were both fine; the error surfaced at close and the
    endpoint turned it into a trailing SSE `error` event after a good response.
    Caught live against the real SDK, not by the fakes — hence this test.
    """
    import concurrent.futures as cf

    events = []
    raw = instrument(_fake_anthropic(deltas=("a", "b", "c")), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")
    gen = client.stream(model="m", messages=MESSAGES, session_id="sess-x")

    def step():
        return next(gen)

    # Each thread gets its own context — the condition that triggered the bug.
    with cf.ThreadPoolExecutor(max_workers=1) as a:
        assert a.submit(step).result() == "a"
    with cf.ThreadPoolExecutor(max_workers=1) as b:
        assert b.submit(step).result() == "b"
        b.submit(gen.close).result()  # close in a third, different context

    (log,) = events
    assert log.status == "cancelled"
    assert log.session_id == "sess-x"  # still captured correctly


def test_ambient_session_does_not_leak_after_a_stream_is_cancelled():
    from sdk.instrument import _session

    events = []
    raw = instrument(_fake_anthropic(deltas=("a", "b", "c")), "anthropic", events.append)
    client = ProviderClient(raw, "anthropic")

    gen = client.stream(model="m", messages=MESSAGES, session_id="sess-10")
    assert next(gen) == "a"
    gen.close()

    assert _session.get() is None
    assert events[0].status == "cancelled"


# ── failure transparency ─────────────────────────────────────────────────────

def test_provider_errors_propagate_unchanged():
    def boom(**kw):
        raise RuntimeError("provider down")

    client = ProviderClient(
        SimpleNamespace(messages=SimpleNamespace(create=boom, stream=boom)),
        "anthropic",
    )
    with pytest.raises(RuntimeError, match="provider down"):
        client.chat(model="m", messages=MESSAGES)
