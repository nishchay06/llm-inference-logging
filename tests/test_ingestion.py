"""Tests for the ingestion service.

We swap the real Postgres session for an in-memory SQLite one (via FastAPI's
dependency override), so these run fast with no database server. We assert the
contract AND that a valid payload is actually stored.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from db.engine import get_session
from db.models import InferenceLogRow
from ingestion.main import app

VALID_LOG = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "status": "success",
    "started_at": "2026-01-01T00:00:00Z",
    "ended_at": "2026-01-01T00:00:01Z",
    "latency_ms": 123.4,
}


@pytest.fixture
def client_and_engine():
    # In-memory SQLite standing in for Postgres — same SQLModel models.
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


def test_valid_payload_is_stored(client_and_engine):
    client, engine = client_and_engine
    resp = client.post("/logs", json=VALID_LOG)
    assert resp.status_code == 200
    assert resp.json()["status"] == "stored"

    with Session(engine) as session:
        rows = session.exec(select(InferenceLogRow)).all()
    assert len(rows) == 1
    assert rows[0].model == "claude-sonnet-5"
    assert rows[0].status == "success"


def test_malformed_payload_is_rejected(client_and_engine):
    client, _ = client_and_engine
    resp = client.post("/logs", json={"provider": "anthropic"})  # missing fields
    assert resp.status_code == 422
