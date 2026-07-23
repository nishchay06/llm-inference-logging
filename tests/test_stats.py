"""Tests for the ingestion /stats endpoint — latency / throughput / errors.

In-memory SQLite via dependency override, same as test_ingestion.py.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from db.engine import get_session
from db.models import InferenceLogRow
from ingestion.main import app


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


def _log(engine, *, status, latency, started, itok=10, otok=5):
    with Session(engine) as s:
        s.add(
            InferenceLogRow(
                session_id="s",
                provider="anthropic",
                model="claude-sonnet-5",
                status=status,
                error_type=None if status == "success" else "APIError",
                started_at=started,
                ended_at=started,
                latency_ms=latency,
                input_tokens=itok,
                output_tokens=otok,
            )
        )
        s.commit()


def test_stats_empty(client_and_engine):
    client, _ = client_and_engine
    resp = client.get("/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_calls"] == 0
    assert body["error_count"] == 0
    assert body["error_rate"] == 0.0
    assert body["avg_latency_ms"] is None


def test_stats_mixed(client_and_engine):
    client, engine = client_and_engine
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _log(engine, status="success", latency=100.0, started=base)
    _log(engine, status="success", latency=300.0, started=base)
    _log(engine, status="error", latency=200.0, started=base)

    resp = client.get("/stats")
    body = resp.json()
    assert body["total_calls"] == 3
    assert body["success_count"] == 2
    assert body["error_count"] == 1
    assert body["error_rate"] == pytest.approx(1 / 3)
    assert body["avg_latency_ms"] == pytest.approx(200.0)
    assert body["total_input_tokens"] == 30
    assert body["total_output_tokens"] == 15


def test_stats_since_window_excludes_older(client_and_engine):
    client, engine = client_and_engine
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _log(engine, status="success", latency=100.0, started=old)
    _log(engine, status="success", latency=500.0, started=new)

    resp = client.get("/stats", params={"since": "2026-03-01T00:00:00Z"})
    body = resp.json()
    assert body["total_calls"] == 1
    assert body["avg_latency_ms"] == pytest.approx(500.0)
