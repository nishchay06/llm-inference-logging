"""Tests for the capture core (sdk/capture.py).

This is the single implementation of *what* a traced LLM call records. Both
instrumentation mechanisms drive it: `TracedClient` wraps a client and calls in,
`instrument()` patches a client's methods to call in. Testing it directly — rather
than only through the two mechanisms — is what lets streaming semantics (TTFT,
cancelled, error) be defined once and asserted once.

No network, no provider SDKs: the core deals in callables and deltas.
"""

import pytest

from sdk.capture import StreamCapture, capture_call, tee_stream
from sdk.providers import ChatResult


def _parse_ok(response):
    return ChatResult(
        text=response["text"],
        model=response["model"],
        input_tokens=response["in"],
        output_tokens=response["out"],
        raw=response,
    )


RESPONSE = {"text": "hi there", "model": "claude-sonnet-5", "in": 7, "out": 2}


# ── capture_call ─────────────────────────────────────────────────────────────

def test_capture_call_emits_one_success_log_and_returns_both_shapes():
    events = []
    response, parsed = capture_call(
        lambda: RESPONSE,
        provider="anthropic",
        model="claude-sonnet-5",
        sink=events.append,
        session_id="s1",
        input_preview="hello",
        parse=_parse_ok,
    )

    # Returns the raw response (for the patch, which must pass it through
    # untouched) AND the normalized result (for the wrapper).
    assert response is RESPONSE
    assert parsed.text == "hi there"

    (log,) = events
    assert log.status == "success"
    assert log.provider == "anthropic"
    assert log.model == "claude-sonnet-5"
    assert log.session_id == "s1"
    assert log.input_tokens == 7 and log.output_tokens == 2
    assert log.input_preview == "hello"
    assert log.output_preview == "hi there"
    assert log.latency_ms >= 0
    assert log.ttft_ms is None  # non-streaming has no time-to-first-token
    assert log.ended_at >= log.started_at


def test_capture_call_records_error_then_reraises():
    events = []
    boom = RuntimeError("upstream exploded")

    with pytest.raises(RuntimeError, match="upstream exploded"):
        capture_call(
            lambda: (_ for _ in ()).throw(boom),
            provider="anthropic",
            model="claude-sonnet-5",
            sink=events.append,
            session_id="s1",
            input_preview="hello",
            parse=_parse_ok,
        )

    (log,) = events
    assert log.status == "error"
    assert log.error_type == "RuntimeError"
    assert "upstream exploded" in log.error_message
    # No usage on a failed call — nothing came back.
    assert log.input_tokens is None and log.output_tokens is None


def test_capture_call_redacts_previews():
    events = []
    capture_call(
        lambda: {**RESPONSE, "text": "mail me at bob@example.com"},
        provider="anthropic",
        model="m",
        sink=events.append,
        session_id=None,
        input_preview="my card is 4111 1111 1111 1111",
        parse=_parse_ok,
    )
    (log,) = events
    assert "[CARD]" in log.input_preview and "4111" not in log.input_preview
    assert "[EMAIL]" in log.output_preview and "bob@example.com" not in log.output_preview


def test_capture_call_prefers_the_model_the_provider_reports():
    """We ask for an alias; the provider answers with what actually served."""
    events = []
    capture_call(
        lambda: {**RESPONSE, "model": "claude-sonnet-5-20260101"},
        provider="anthropic",
        model="claude-sonnet-5",
        sink=events.append,
        session_id=None,
        input_preview="x",
        parse=_parse_ok,
    )
    assert events[0].model == "claude-sonnet-5-20260101"


# ── StreamCapture ────────────────────────────────────────────────────────────

def _capture(events, model="claude-sonnet-5"):
    return StreamCapture(
        provider="anthropic",
        model=model,
        sink=events.append,
        session_id="s1",
        input_preview="hello",
    )


def test_stream_capture_stamps_ttft_on_the_first_delta_only():
    events = []
    cap = _capture(events)
    assert cap.ttft_ms is None

    cap.on_delta("Hel")
    first = cap.ttft_ms
    assert first is not None

    cap.on_delta("lo")
    assert cap.ttft_ms == first  # unchanged by later deltas


def test_stream_capture_success_emits_one_log_with_usage_and_ttft():
    events = []
    cap = _capture(events)
    cap.on_delta("Hel")
    cap.on_delta("lo")
    cap.emit_success(model="claude-sonnet-5", input_tokens=5, output_tokens=3)

    (log,) = events
    assert log.status == "success"
    assert log.output_preview == "Hello"  # accumulated from the deltas
    assert log.input_tokens == 5 and log.output_tokens == 3
    assert log.ttft_ms is not None
    assert log.ttft_ms <= log.latency_ms


def test_stream_capture_cancelled_keeps_the_partial_output():
    events = []
    cap = _capture(events)
    cap.on_delta("Hel")
    cap.emit_cancelled()

    (log,) = events
    assert log.status == "cancelled"
    assert log.output_preview == "Hel"
    assert log.ttft_ms is not None  # a first token did arrive before the abort
    # Usage is unknown for an aborted stream — the provider never reported it.
    assert log.input_tokens is None and log.output_tokens is None


def test_stream_capture_cancelled_before_any_token_has_no_ttft():
    events = []
    cap = _capture(events)
    cap.emit_cancelled()

    (log,) = events
    assert log.status == "cancelled"
    assert log.ttft_ms is None
    assert log.output_preview in (None, "")


def test_stream_capture_error_records_type_and_message():
    events = []
    cap = _capture(events)
    cap.on_delta("Hel")
    cap.emit_error(ValueError("stream died"))

    (log,) = events
    assert log.status == "error"
    assert log.error_type == "ValueError"
    assert "stream died" in log.error_message


def test_stream_capture_emits_at_most_once():
    """Both mechanisms have a close path that could fire twice (a generator's
    finally plus a context manager's __exit__). Exactly one log per call is the
    invariant the dashboard's counts depend on."""
    events = []
    cap = _capture(events)
    cap.on_delta("hi")
    cap.emit_success(model="m", input_tokens=1, output_tokens=1)
    cap.emit_success(model="m", input_tokens=1, output_tokens=1)
    cap.emit_cancelled()

    assert len(events) == 1


def test_stream_capture_redacts_accumulated_output():
    events = []
    cap = _capture(events)
    cap.on_delta("write to bob@")
    cap.on_delta("example.com now")
    cap.emit_success(model="m", input_tokens=1, output_tokens=1)

    (log,) = events
    # The address spans two deltas — redaction must run on the joined text, not
    # per-delta, or the split would hide it.
    assert "[EMAIL]" in log.output_preview
    assert "bob@example.com" not in log.output_preview


# ── tee_stream (the adapter-generator shape) ─────────────────────────────────

def _adapter_gen(deltas, final=None, error=None):
    """Mimics an adapter's stream(): yields str deltas, returns a ChatResult."""

    def gen():
        for d in deltas:
            if error is not None and d == "BOOM":
                raise error
            yield d
        return final

    return gen()


def test_tee_stream_passes_deltas_through_and_logs_once_at_the_end():
    events = []
    final = ChatResult(text="Hello", model="m2", input_tokens=5, output_tokens=3)
    out = list(
        tee_stream(
            _adapter_gen(["Hel", "lo"], final=final),
            provider="anthropic",
            model="m",
            sink=events.append,
            session_id="s1",
            input_preview="hi",
        )
    )

    assert out == ["Hel", "lo"]  # observe-only: deltas unchanged
    (log,) = events
    assert log.status == "success"
    assert log.model == "m2"  # what the provider reported wins
    assert log.input_tokens == 5 and log.output_tokens == 3
    assert log.ttft_ms is not None


def test_tee_stream_marks_cancelled_when_the_consumer_stops_early():
    events = []
    gen = tee_stream(
        _adapter_gen(["a", "b", "c"]),
        provider="anthropic",
        model="m",
        sink=events.append,
        session_id="s1",
        input_preview="hi",
    )
    assert next(gen) == "a"
    gen.close()  # client disconnected

    (log,) = events
    assert log.status == "cancelled"
    assert log.output_preview == "a"


def test_tee_stream_records_error_then_reraises():
    events = []
    gen = tee_stream(
        _adapter_gen(["a", "BOOM"], error=RuntimeError("mid-stream")),
        provider="anthropic",
        model="m",
        sink=events.append,
        session_id="s1",
        input_preview="hi",
    )
    with pytest.raises(RuntimeError, match="mid-stream"):
        list(gen)

    (log,) = events
    assert log.status == "error"
    assert log.error_type == "RuntimeError"
    assert log.output_preview == "a"  # whatever made it through is kept


def test_tee_stream_falls_back_to_accumulated_text_when_no_final_result():
    """A provider that reports no final usage still gets a usable log."""
    events = []
    list(
        tee_stream(
            _adapter_gen(["Hel", "lo"], final=None),
            provider="anthropic",
            model="m",
            sink=events.append,
            session_id=None,
            input_preview="hi",
        )
    )
    (log,) = events
    assert log.status == "success"
    assert log.model == "m"  # requested model, since none was reported
    assert log.output_preview == "Hello"
    assert log.input_tokens is None
