"""Tests for the chatbot read endpoints — list and resume conversations.

Same approach as test_ingestion.py: an in-memory SQLite engine stands in for
Postgres via FastAPI's dependency override, so these run fast with no server.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.main import app
from db.engine import get_session
from db.models import Conversation, Message


@pytest.fixture
def client_and_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def _seed(engine, session_id, created, updated, turns):
    """Seed one conversation with (role, content) turns."""
    with Session(engine) as s:
        s.add(Conversation(session_id=session_id, created_at=created, updated_at=updated))
        for role, content in turns:
            s.add(Message(session_id=session_id, role=role, content=content))
        s.commit()


def test_list_empty(client_and_engine):
    client, _ = client_and_engine
    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_orders_by_updated_desc_with_preview_and_count(client_and_engine):
    client, engine = client_and_engine
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    _seed(engine, "old", older, older, [("user", "first question"), ("assistant", "a1")])
    _seed(
        engine,
        "new",
        newer,
        newer,
        [("user", "hello there"), ("assistant", "hi"), ("user", "again"), ("assistant", "ok")],
    )

    resp = client.get("/conversations")
    assert resp.status_code == 200
    rows = resp.json()

    # Most-recently-updated first.
    assert [r["session_id"] for r in rows] == ["new", "old"]
    assert rows[0]["message_count"] == 4
    assert rows[1]["message_count"] == 2
    # Preview is the first *user* message.
    assert rows[0]["preview"] == "hello there"
    assert rows[1]["preview"] == "first question"


def test_resume_returns_messages_in_order(client_and_engine):
    client, engine = client_and_engine
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _seed(
        engine,
        "abc",
        ts,
        ts,
        [("user", "hi"), ("assistant", "hello"), ("user", "bye"), ("assistant", "later")],
    )

    resp = client.get("/conversations/abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "abc"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "hi"),
        ("assistant", "hello"),
        ("user", "bye"),
        ("assistant", "later"),
    ]


def test_resume_unknown_session_is_404(client_and_engine):
    client, _ = client_and_engine
    resp = client.get("/conversations/does-not-exist")
    assert resp.status_code == 404
