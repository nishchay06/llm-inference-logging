"""Tests for the provider adapters — the multi-provider seam.

Each adapter isolates one provider's differences: how to CALL it, and how to
PARSE its response into a normalized ChatResult. We test both with fake,
provider-shaped stubs — no real API key or network needed.
"""

from types import SimpleNamespace

from sdk.providers import AnthropicAdapter, GeminiAdapter, ChatResult


# ── Anthropic ────────────────────────────────────────────────────────────────

def _anthropic_response(text="hi", model="claude-sonnet-5", in_tok=7, out_tok=2):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)
    return SimpleNamespace(content=[block], model=model, usage=usage)


class _FakeAnthropic:
    def __init__(self, response):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_anthropic_parse_normalizes():
    result = AnthropicAdapter().parse(_anthropic_response(text="hello", in_tok=5, out_tok=3))
    assert isinstance(result, ChatResult)
    assert result.text == "hello"
    assert result.model == "claude-sonnet-5"
    assert result.input_tokens == 5
    assert result.output_tokens == 3


def test_anthropic_create_forwards_to_messages_create():
    client = _FakeAnthropic(_anthropic_response())
    adapter = AnthropicAdapter()
    adapter.create(
        client,
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 64
    assert call["messages"] == [{"role": "user", "content": "hi"}]


# ── Gemini ───────────────────────────────────────────────────────────────────

def _gemini_response(text="hey", model="gemini-2.0-flash", prompt=11, cand=4):
    usage = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand)
    return SimpleNamespace(text=text, usage_metadata=usage, model_version=model)


class _FakeGemini:
    """Duck-types the google-genai client: client.models.generate_content(...)."""

    def __init__(self, response):
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)
        self._response = response

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_gemini_parse_normalizes_divergent_fields():
    result = GeminiAdapter().parse(_gemini_response(text="hello", prompt=11, cand=4))
    assert result.text == "hello"
    assert result.model == "gemini-2.0-flash"
    # prompt_token_count / candidates_token_count → normalized input/output
    assert result.input_tokens == 11
    assert result.output_tokens == 4


def test_gemini_create_translates_message_format():
    client = _FakeGemini(_gemini_response())
    adapter = GeminiAdapter()
    adapter.create(
        client,
        model="gemini-2.0-flash",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ],
        max_tokens=64,
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "gemini-2.0-flash"
    # roles mapped (assistant → model) and content wrapped in parts.
    assert call["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
        {"role": "user", "parts": [{"text": "bye"}]},
    ]
    # max_tokens mapped into the generation config as max_output_tokens.
    assert getattr(call["config"], "max_output_tokens", None) == 64
