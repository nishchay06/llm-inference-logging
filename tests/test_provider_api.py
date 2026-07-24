"""Tests for runtime provider selection (the UI can switch providers).

These need no DB or LLM call: /providers just reports what's configured, and
the unavailable-provider check in /chat happens before any DB write or API call.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_providers_lists_available():
    client = TestClient(app)
    resp = client.get("/providers")
    assert resp.status_code == 200
    body = resp.json()
    names = [p["name"] for p in body["providers"]]
    # conftest sets a dummy ANTHROPIC_API_KEY, so anthropic is always available.
    assert "anthropic" in names
    assert body["default"] == "anthropic"
    # every listed provider advertises a model.
    assert all(p.get("model") for p in body["providers"])


def test_chat_rejects_unknown_provider():
    client = TestClient(app)
    # "openai" is not a registered provider → rejected before any DB/LLM work.
    resp = client.post("/chat", json={"message": "hi", "provider": "openai"})
    assert resp.status_code == 400
