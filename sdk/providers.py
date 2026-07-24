"""Provider adapters — the multi-provider seam.

Each adapter isolates ONE provider's differences behind a uniform interface, so
`TracedClient` and the chat code stay provider-agnostic:

- `create(client, ...)` — how to CALL this provider (method + argument shape).
- `parse(response) -> ChatResult` — how to READ its response into normalized
  fields (text, model, token usage).

Adding a provider = add an adapter + register it. Nothing else changes. (In
production you'd likely reach for a library like `litellm` that does this
normalization for you; hand-rolling it here is the point of the exercise.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChatResult:
    """Normalized result of a chat call — the same shape for every provider."""

    text: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw: Any = None  # the underlying provider response, for escape-hatch access


class AnthropicAdapter:
    provider = "anthropic"

    def create(self, client, *, model, messages, max_tokens, **kwargs):
        return client.messages.create(
            model=model, messages=messages, max_tokens=max_tokens, **kwargs
        )

    def parse(self, response) -> ChatResult:
        text = next((b.text for b in response.content if b.type == "text"), "")
        return ChatResult(
            text=text,
            model=getattr(response, "model", None),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw=response,
        )

    def stream(self, client, *, model, messages, max_tokens, **kwargs):
        """Yield text deltas, then `return` a ChatResult with usage from the
        final message. Uses the SDK's stream helper (text_stream + the final
        aggregated message)."""
        parts: list[str] = []
        with client.messages.stream(
            model=model, messages=messages, max_tokens=max_tokens, **kwargs
        ) as stream:
            for delta in stream.text_stream:
                parts.append(delta)
                yield delta
            final = stream.get_final_message()
        return ChatResult(
            text="".join(parts),
            model=getattr(final, "model", None),
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
            raw=final,
        )


class GeminiAdapter:
    provider = "gemini"

    def create(self, client, *, model, messages, max_tokens, **kwargs):
        # Gemini diverges: roles are user/model, content is wrapped in `parts`,
        # and max tokens goes through a config object — so the adapter TRANSLATES
        # the canonical message list rather than passing it through.
        from google.genai import types

        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        return client.models.generate_content(
            model=model,
            contents=_to_gemini_contents(messages),
            config=config,
            **kwargs,
        )

    def parse(self, response) -> ChatResult:
        usage = getattr(response, "usage_metadata", None)
        return ChatResult(
            text=getattr(response, "text", "") or "",
            model=getattr(response, "model_version", None),
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            raw=response,
        )

    def stream(self, client, *, model, messages, max_tokens, **kwargs):
        """Yield text deltas from the streaming chunks; usage_metadata arrives on
        the final chunk(s), model in model_version."""
        from google.genai import types

        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        parts: list[str] = []
        model_version = None
        input_tokens = output_tokens = None
        for chunk in client.models.generate_content_stream(
            model=model,
            contents=_to_gemini_contents(messages),
            config=config,
            **kwargs,
        ):
            text = getattr(chunk, "text", None)
            if text:
                parts.append(text)
                yield text
            model_version = getattr(chunk, "model_version", None) or model_version
            usage = getattr(chunk, "usage_metadata", None)
            if usage is not None:
                input_tokens = getattr(usage, "prompt_token_count", None)
                output_tokens = getattr(usage, "candidates_token_count", None)
        return ChatResult(
            text="".join(parts),
            model=model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=None,
        )


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    role_map = {"user": "user", "assistant": "model"}
    return [
        {"role": role_map.get(m["role"], "user"), "parts": [{"text": m["content"]}]}
        for m in messages
    ]


# Registry: provider name → adapter instance (adapters are stateless).
ADAPTERS = {
    AnthropicAdapter.provider: AnthropicAdapter(),
    GeminiAdapter.provider: GeminiAdapter(),
}

# Sensible default model per provider (overridable via LLM_MODEL).
# gemini-flash-latest tracks the current flash model (resolves to e.g.
# gemini-3.6-flash) and has broader free-tier availability than the pinned
# gemini-2.0-flash.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-flash-latest",
}


def build_client(provider: str):
    """Construct the underlying provider SDK client. Imported lazily so we only
    pull in the SDK we actually use."""
    if provider == "anthropic":
        from anthropic import Anthropic

        return Anthropic()
    if provider == "gemini":
        import os

        from google import genai

        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    raise ValueError(f"unknown provider: {provider!r}")
