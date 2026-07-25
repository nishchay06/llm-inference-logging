"""How the app wires instrumentation, and proof the two mechanisms agree.

Patching is the default. That is only defensible if switching the default cannot
lose or change telemetry, so the load-bearing tests here are the **parity** ones:
for the same call, the patch path and the wrapper path must record the same
InferenceLog fields. If they ever diverge, the default is not safe to flip and
these fail.

`LLM_INSTRUMENTATION=wrapper` is the documented rollback, so it is tested too — a
rollback nobody exercises is not a rollback.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.main as app_main
from conftest import make_engine
from db.models import Message
from sdk.client import ProviderClient
from sdk.tracing import TracedClient


# ── fakes ────────────────────────────────────────────────────────────────────

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
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                model="claude-sonnet-5",
                usage=SimpleNamespace(input_tokens=5, output_tokens=3),
            ),
            stream=lambda **kw: _FakeStream(list(deltas), final),
        )
    )


def _only_anthropic(provider):
    """Client factory standing in for build_client: only anthropic is configured."""
    if provider == "anthropic":
        return _fake_anthropic()
    raise RuntimeError(f"{provider} has no API key configured")


MESSAGES = [{"role": "user", "content": "hello"}]


def _stable_fields(log):
    """Everything except identity and timing, which legitimately differ per call."""
    return {
        "provider": log.provider,
        "model": log.model,
        "status": log.status,
        "session_id": log.session_id,
        "input_tokens": log.input_tokens,
        "output_tokens": log.output_tokens,
        "input_preview": log.input_preview,
        "output_preview": log.output_preview,
        "error_type": log.error_type,
        "error_message": log.error_message,
    }


# ── configuration ────────────────────────────────────────────────────────────

def test_default_instrumentation_is_patch():
    """The whole point of the change: auto-instrumentation is what ships by
    default. Asserted against the declared default rather than the resolved value,
    so the test states intent even when an env var overrides it locally."""
    assert app_main.DEFAULT_INSTRUMENTATION == "patch"


def test_unset_environment_selects_the_patch_path(monkeypatch):
    """The default is not just a string — it must actually build patched clients."""
    monkeypatch.delenv("LLM_INSTRUMENTATION", raising=False)
    monkeypatch.setattr(app_main, "INSTRUMENTATION", app_main.DEFAULT_INSTRUMENTATION)

    clients, _ = app_main.build_clients(
        sink=lambda e: None, client_factory=_only_anthropic
    )
    assert isinstance(clients["anthropic"], ProviderClient)


def test_patch_mode_builds_normalizing_clients_over_patched_sdks():
    events = []
    clients, models = app_main.build_clients(
        sink=events.append, mode="patch", client_factory=_only_anthropic
    )

    client = clients["anthropic"]
    assert isinstance(client, ProviderClient)
    # The underlying SDK object is patched — that is where capture comes from.
    assert getattr(client.raw.messages.create, "_auto_instrumented", False)
    assert getattr(client.raw.messages.stream, "_auto_instrumented", False)
    assert models["anthropic"]


def test_wrapper_mode_builds_traced_clients():
    """The rollback path."""
    events = []
    clients, _ = app_main.build_clients(
        sink=events.append, mode="wrapper", client_factory=_only_anthropic
    )
    assert isinstance(clients["anthropic"], TracedClient)


def test_a_provider_without_credentials_is_skipped_not_fatal():
    events = []
    clients, models = app_main.build_clients(
        sink=events.append, mode="patch", client_factory=_only_anthropic
    )
    assert "anthropic" in clients
    assert "gemini" not in clients and "gemini" not in models


# ── parity: the assertion the default rests on ───────────────────────────────

def test_both_modes_record_equivalent_logs_for_a_chat():
    patched, wrapped = [], []
    p_clients, _ = app_main.build_clients(
        sink=patched.append, mode="patch", client_factory=_only_anthropic
    )
    w_clients, _ = app_main.build_clients(
        sink=wrapped.append, mode="wrapper", client_factory=_only_anthropic
    )

    for clients in (p_clients, w_clients):
        clients["anthropic"].chat(
            model="claude-sonnet-5", messages=MESSAGES, session_id="s1"
        )

    (p_log,), (w_log,) = patched, wrapped
    assert _stable_fields(p_log) == _stable_fields(w_log)
    assert p_log.status == "success"
    assert p_log.output_preview == "hi there"


def test_both_modes_record_equivalent_logs_for_a_stream():
    patched, wrapped = [], []
    p_clients, _ = app_main.build_clients(
        sink=patched.append, mode="patch", client_factory=_only_anthropic
    )
    w_clients, _ = app_main.build_clients(
        sink=wrapped.append, mode="wrapper", client_factory=_only_anthropic
    )

    for clients in (p_clients, w_clients):
        deltas = list(
            clients["anthropic"].stream(
                model="claude-sonnet-5", messages=MESSAGES, session_id="s1"
            )
        )
        assert deltas == ["Hel", "lo"]

    (p_log,), (w_log,) = patched, wrapped
    assert _stable_fields(p_log) == _stable_fields(w_log)
    # TTFT is the field most at risk of being lost by the switch.
    assert p_log.ttft_ms is not None and w_log.ttft_ms is not None


def test_both_modes_record_equivalent_logs_for_a_cancelled_stream():
    patched, wrapped = [], []
    p_clients, _ = app_main.build_clients(
        sink=patched.append, mode="patch", client_factory=_only_anthropic
    )
    w_clients, _ = app_main.build_clients(
        sink=wrapped.append, mode="wrapper", client_factory=_only_anthropic
    )

    for clients in (p_clients, w_clients):
        gen = clients["anthropic"].stream(
            model="claude-sonnet-5", messages=MESSAGES, session_id="s1"
        )
        assert next(gen) == "Hel"
        gen.close()

    (p_log,), (w_log,) = patched, wrapped
    assert _stable_fields(p_log) == _stable_fields(w_log)
    assert p_log.status == "cancelled"


def test_patch_mode_records_exactly_one_log_per_call():
    """Guards double counting — patch and wrapper must never both be active."""
    events = []
    clients, _ = app_main.build_clients(
        sink=events.append, mode="patch", client_factory=_only_anthropic
    )
    clients["anthropic"].chat(model="m", messages=MESSAGES, session_id="s1")
    assert len(events) == 1


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def app_with_fake_provider(monkeypatch):
    """Point the app at a test database and a fake, patched provider.

    /chat writes through a module-global engine rather than dependency injection,
    so the engine is monkeypatched here.
    """
    engine = make_engine()
    monkeypatch.setattr(app_main, "engine", engine)

    events = []
    clients, models = app_main.build_clients(
        sink=events.append, mode="patch", client_factory=_only_anthropic
    )
    monkeypatch.setattr(app_main, "CLIENTS", clients)
    monkeypatch.setattr(app_main, "MODEL_FOR", models)
    monkeypatch.setattr(app_main, "DEFAULT_PROVIDER", "anthropic")

    return TestClient(app_main.app), events, engine


def test_chat_endpoint_is_auto_instrumented(app_with_fake_provider):
    client, events, engine = app_with_fake_provider

    resp = client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "hi there"

    (log,) = events
    assert log.status == "success"
    assert log.session_id == resp.json()["session_id"]
    assert log.input_preview == "hello"

    # and the conversation was persisted
    with Session(engine) as db:
        roles = [m.role for m in db.exec(select(Message)).all()]
    assert roles == ["user", "assistant"]


def test_chat_stream_endpoint_is_auto_instrumented(app_with_fake_provider):
    """The case that would have silently lost telemetry if the default had been
    flipped before the patch layer handled streams."""
    client, events, engine = app_with_fake_provider

    with client.stream("POST", "/chat/stream", json={"message": "hello"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    assert "Hel" in body and "lo" in body

    (log,) = events
    assert log.status == "success"
    assert log.ttft_ms is not None
    assert log.output_preview == "Hello"

    with Session(engine) as db:
        stored = db.exec(select(Message).where(Message.role == "assistant")).all()
    assert [m.content for m in stored] == ["Hello"]
