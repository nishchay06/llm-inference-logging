"""Tests for auto-instrumentation (sdk/instrument.py).

Monkey-patch a provider client so a plain call is captured — with fakes, no
network. The wrapper only observes: it returns the raw response unchanged and
emits one InferenceLog via the sink.
"""

from types import SimpleNamespace

import pytest

from sdk.instrument import instrument, session_scope


# ── fakes ────────────────────────────────────────────────────────────────────

def _anthropic_response(text="hi there", model="claude-sonnet-5", in_tok=7, out_tok=2):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)
    return SimpleNamespace(content=[block], model=model, usage=usage)


class _FakeMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _fake_anthropic(response=None, error=None):
    return SimpleNamespace(messages=_FakeMessages(response, error))


def _fake_gemini(text="bonjour", model="gemini-3.6-flash", prompt=9, cand=4):
    resp = SimpleNamespace(
        text=text,
        model_version=model,
        usage_metadata=SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand),
    )
    return SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))


# ── capture + transparency ───────────────────────────────────────────────────

def test_plain_call_is_captured_and_response_passthrough():
    events = []
    resp = _anthropic_response(text="the answer", in_tok=11, out_tok=4)
    client = instrument(_fake_anthropic(response=resp), "anthropic", events.append)

    out = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "hello"}],
    )

    # transparent: the caller gets the RAW provider response back, not a ChatResult
    assert out is resp
    assert len(events) == 1
    log = events[0]
    assert log.status == "success"
    assert log.provider == "anthropic"
    assert log.model == "claude-sonnet-5"
    assert log.input_tokens == 11 and log.output_tokens == 4
    assert log.input_preview == "hello"
    assert log.output_preview == "the answer"


def test_error_is_captured_and_reraised():
    events = []
    client = instrument(_fake_anthropic(error=ValueError("boom")), "anthropic", events.append)
    with pytest.raises(ValueError):
        client.messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert len(events) == 1 and events[0].status == "error"
    assert events[0].error_type == "ValueError"


def test_session_scope_threads_session_id():
    events = []
    client = instrument(_fake_anthropic(response=_anthropic_response()), "anthropic", events.append)
    with session_scope("sess-42"):
        client.messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert events[0].session_id == "sess-42"
    # outside the scope, no ambient session
    client.messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert events[1].session_id is None


def test_gemini_generate_content_is_captured():
    events = []
    client = instrument(_fake_gemini(), "gemini", events.append)
    out = client.models.generate_content(
        model="gemini-flash-latest",
        contents=[{"role": "user", "parts": [{"text": "hi"}]}],
    )
    assert out.text == "bonjour"
    log = events[0]
    assert log.provider == "gemini" and log.model == "gemini-3.6-flash"
    assert log.input_tokens == 9 and log.output_tokens == 4
    assert log.output_preview == "bonjour"


def test_instrument_is_idempotent():
    events = []
    client = _fake_anthropic(response=_anthropic_response())
    instrument(client, "anthropic", events.append)
    instrument(client, "anthropic", events.append)  # second time is a no-op
    client.messages.create(model="claude-sonnet-5", max_tokens=10, messages=[])
    assert len(events) == 1  # one call → one log, not two


def test_preview_is_pii_redacted():
    events = []
    client = instrument(
        _fake_anthropic(response=_anthropic_response(text="sent to a@b.com")),
        "anthropic",
        events.append,
    )
    client.messages.create(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": "my ssn is 123-45-6789"}],
    )
    log = events[0]
    assert "[SSN]" in log.input_preview and "123-45-6789" not in log.input_preview
    assert "[EMAIL]" in log.output_preview


def test_patches_the_real_anthropic_sdk_surface():
    # proves it patches the actual library structure, not just our fakes. No
    # network: we assert the method was replaced, we don't call it.
    import os

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
    from anthropic import Anthropic

    client = instrument(Anthropic(), "anthropic", lambda e: None)
    assert getattr(client.messages.create, "_auto_instrumented", False) is True
