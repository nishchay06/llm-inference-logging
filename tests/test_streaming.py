"""Tests for streaming: the adapter stream methods and the TracedClient.stream
wrapper. Fakes only — no real API, no network.

The wrapper tees the stream (yields deltas onward while accumulating) and emits
exactly one InferenceLog when the stream ends: success on completion, cancelled
on generator close, error on exception.
"""

from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from sdk.providers import AnthropicAdapter, GeminiAdapter, ChatResult
from sdk.tracing import TracedClient
from db.models import InferenceLogRow


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeAnthropicStreamCM:
    """Stand-in for the object returned by client.messages.stream(...)."""

    def __init__(self, deltas, final, error=None):
        self._deltas = deltas
        self._final = final
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for d in self._deltas:
            yield d

    def get_final_message(self):
        return self._final


def _fake_anthropic_client(deltas=("Hel", "lo"), in_tok=5, out_tok=3, error=None):
    final = SimpleNamespace(
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )
    cm = _FakeAnthropicStreamCM(list(deltas), final)

    def stream(**kwargs):
        if error is not None:
            raise error
        return cm

    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def _fake_gemini_client(deltas=("Bon", "jour"), prompt=9, cand=4):
    chunks = []
    for i, d in enumerate(deltas):
        last = i == len(deltas) - 1
        chunks.append(
            SimpleNamespace(
                text=d,
                model_version="gemini-3.6-flash",
                usage_metadata=(
                    SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand)
                    if last
                    else None
                ),
            )
        )

    def generate_content_stream(**kwargs):
        return iter(chunks)

    return SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate_content_stream))


def _drain(gen):
    """Run a generator to completion, returning (yielded_list, return_value)."""
    out = []
    try:
        while True:
            out.append(next(gen))
    except StopIteration as stop:
        return out, stop.value


# ── adapter.stream ───────────────────────────────────────────────────────────

def test_anthropic_stream_yields_deltas_and_returns_result():
    client = _fake_anthropic_client(deltas=("Hel", "lo"), in_tok=5, out_tok=3)
    deltas, result = _drain(
        AnthropicAdapter().stream(
            client, model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}], max_tokens=10
        )
    )
    assert deltas == ["Hel", "lo"]
    assert isinstance(result, ChatResult)
    assert result.text == "Hello"
    assert result.model == "claude-sonnet-5"
    assert result.input_tokens == 5
    assert result.output_tokens == 3


def test_gemini_stream_yields_deltas_and_normalizes_usage():
    client = _fake_gemini_client(deltas=("Bon", "jour"), prompt=9, cand=4)
    deltas, result = _drain(
        GeminiAdapter().stream(
            client, model="gemini-flash-latest", messages=[{"role": "user", "content": "hi"}], max_tokens=10
        )
    )
    assert deltas == ["Bon", "jour"]
    assert result.text == "Bonjour"
    assert result.model == "gemini-3.6-flash"
    assert result.input_tokens == 9
    assert result.output_tokens == 4


# ── TracedClient.stream ──────────────────────────────────────────────────────

def test_stream_success_emits_one_log_with_ttft():
    events = []
    client = _fake_anthropic_client(deltas=("Hel", "lo"), in_tok=5, out_tok=3)
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    out = list(
        traced.stream(
            model="claude-sonnet-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
            session_id="s1",
        )
    )
    assert out == ["Hel", "lo"]
    assert len(events) == 1
    log = events[0]
    assert log.status == "success"
    assert log.provider == "anthropic"
    assert log.model == "claude-sonnet-5"
    assert log.output_preview == "Hello"
    assert log.input_tokens == 5
    assert log.output_tokens == 3
    assert log.ttft_ms is not None and log.ttft_ms >= 0


def test_stream_cancel_emits_cancelled_log_with_partial_text():
    events = []
    client = _fake_anthropic_client(deltas=("Hel", "lo"))
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    gen = traced.stream(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "hi"}],
        session_id="s2",
    )
    assert next(gen) == "Hel"  # consume one delta, then cancel
    gen.close()

    assert len(events) == 1
    log = events[0]
    assert log.status == "cancelled"
    assert log.output_preview == "Hel"  # only what was streamed so far


def test_stream_error_emits_error_log_and_reraises():
    events = []
    client = _fake_anthropic_client(error=ValueError("boom"))
    traced = TracedClient(client, provider="anthropic", sink=events.append)

    with pytest.raises(ValueError):
        list(
            traced.stream(
                model="claude-sonnet-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
                session_id="s3",
            )
        )
    assert len(events) == 1
    assert events[0].status == "error"
    assert events[0].error_type == "ValueError"


# ── schema: ttft_ms column ───────────────────────────────────────────────────

def test_inference_log_row_stores_ttft():
    from datetime import datetime, timezone

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            InferenceLogRow(
                session_id="s",
                provider="anthropic",
                model="claude-sonnet-5",
                status="success",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                latency_ms=100.0,
                ttft_ms=42.0,
            )
        )
        s.commit()
    with Session(engine) as s:
        row = s.exec(select(InferenceLogRow)).one()
    assert row.ttft_ms == 42.0
