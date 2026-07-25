"""Tests for the dashboard/observability endpoints on the ingestion service:
extended /stats (percentiles), /stats/timeseries, /stats/by_model, and the
/logs query endpoint. In-memory SQLite via dependency override — no server.
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
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def _add(engine, **over):
    base = dict(
        session_id="s",
        provider="anthropic",
        model="claude-sonnet-5",
        status="success",
        error_type=None,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        latency_ms=100.0,
        input_tokens=10,
        output_tokens=5,
        input_preview="hello world",
        output_preview="hi there",
    )
    base.update(over)
    base["ended_at"] = base["started_at"]
    with Session(engine) as s:
        s.add(InferenceLogRow(**base))
        s.commit()


# ── /stats percentiles ───────────────────────────────────────────────────────

def test_stats_percentiles(client_and_engine):
    client, engine = client_and_engine
    for lat in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        _add(engine, latency_ms=float(lat))
    body = client.get("/stats").json()
    assert body["total_calls"] == 10
    assert body["avg_latency_ms"] == pytest.approx(55.0)
    # nearest-rank percentiles
    assert body["p50_ms"] == pytest.approx(50.0)
    assert body["p95_ms"] == pytest.approx(100.0)
    assert body["p99_ms"] == pytest.approx(100.0)


def test_stats_percentiles_null_when_empty(client_and_engine):
    client, _ = client_and_engine
    body = client.get("/stats").json()
    assert body["p50_ms"] is None and body["p95_ms"] is None


# ── /stats/timeseries ────────────────────────────────────────────────────────

def test_timeseries_buckets(client_and_engine):
    client, engine = client_and_engine
    t = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    _add(engine, started_at=t.replace(second=10), latency_ms=100.0)
    _add(engine, started_at=t.replace(second=40), latency_ms=200.0)
    _add(engine, started_at=t.replace(minute=1, second=20), status="error", error_type="X", latency_ms=300.0)

    body = client.get("/stats/timeseries", params={"bucket": 60}).json()
    pts = body["points"]
    assert len(pts) == 2
    by_calls = {p["calls"]: p for p in pts}
    assert by_calls[2]["errors"] == 0
    assert by_calls[2]["avg_latency_ms"] == pytest.approx(150.0)
    assert by_calls[1]["errors"] == 1


# ── /stats/by_model ──────────────────────────────────────────────────────────

def test_by_model_grouping(client_and_engine):
    client, engine = client_and_engine
    _add(engine, provider="anthropic", model="claude-sonnet-5", latency_ms=100.0)
    _add(engine, provider="anthropic", model="claude-sonnet-5", latency_ms=200.0)
    _add(engine, provider="gemini", model="gemini-3.6-flash", latency_ms=300.0)
    _add(engine, provider="gemini", model="gemini-3.6-flash", status="error", error_type="X", latency_ms=400.0)

    items = client.get("/stats/by_model").json()["items"]
    by_model = {i["model"]: i for i in items}
    assert by_model["claude-sonnet-5"]["calls"] == 2
    assert by_model["claude-sonnet-5"]["error_rate"] == pytest.approx(0.0)
    assert by_model["claude-sonnet-5"]["avg_latency_ms"] == pytest.approx(150.0)
    assert by_model["gemini-3.6-flash"]["calls"] == 2
    assert by_model["gemini-3.6-flash"]["error_rate"] == pytest.approx(0.5)


# ── /logs query ──────────────────────────────────────────────────────────────

def test_logs_empty(client_and_engine):
    client, _ = client_and_engine
    body = client.get("/logs").json()
    assert body["total"] == 0 and body["items"] == []


def test_logs_filter_order_and_paginate(client_and_engine):
    client, engine = client_and_engine
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 5 logs at increasing times; mix of status/provider
    _add(engine, started_at=base.replace(second=1), status="success", provider="anthropic")
    _add(engine, started_at=base.replace(second=2), status="error", error_type="X", provider="anthropic")
    _add(engine, started_at=base.replace(second=3), status="success", provider="gemini")
    _add(engine, started_at=base.replace(second=4), status="cancelled", provider="gemini")
    _add(engine, started_at=base.replace(second=5), status="success", provider="anthropic")

    # all, newest-first
    body = client.get("/logs").json()
    assert body["total"] == 5
    assert len(body["items"]) == 5
    ts = [i["started_at"] for i in body["items"]]
    assert ts == sorted(ts, reverse=True)

    # filter by status
    assert client.get("/logs", params={"status": "error"}).json()["total"] == 1
    # filter by provider
    assert client.get("/logs", params={"provider": "gemini"}).json()["total"] == 2

    # paginate
    page = client.get("/logs", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 5 and len(page["items"]) == 2


def test_stats_honors_status_and_provider_filters(client_and_engine):
    client, engine = client_and_engine
    _add(engine, status="success", provider="anthropic")
    _add(engine, status="success", provider="anthropic")
    _add(engine, status="error", error_type="X", provider="gemini")

    # status filter scopes the overview, not just the stream
    s = client.get("/stats", params={"status": "error"}).json()
    assert s["total_calls"] == 1 and s["error_count"] == 1 and s["error_rate"] == 1.0
    # provider filter too
    s2 = client.get("/stats", params={"provider": "anthropic"}).json()
    assert s2["total_calls"] == 2 and s2["error_count"] == 0


def test_timeseries_and_by_model_honor_filters(client_and_engine):
    client, engine = client_and_engine
    _add(engine, provider="anthropic")
    _add(engine, provider="gemini")
    _add(engine, provider="gemini", status="error", error_type="X")

    ts = client.get("/stats/timeseries", params={"provider": "gemini"}).json()
    assert sum(p["calls"] for p in ts["points"]) == 2
    bm = client.get("/stats/by_model", params={"status": "error"}).json()
    assert all(i["provider"] == "gemini" for i in bm["items"])
    assert sum(i["calls"] for i in bm["items"]) == 1


def test_post_logs_then_get_logs_roundtrip(client_and_engine):
    """The write path (POST /logs → store_log) and the dashboard query path
    (GET /logs) agree end-to-end: a posted log is retrievable via the explorer."""
    client, _ = client_and_engine
    payload = {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "status": "success",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:01Z",
        "latency_ms": 150.0,
        "input_preview": "roundtrip-marker",
    }
    assert client.post("/logs", json=payload).status_code == 200

    body = client.get("/logs", params={"q": "roundtrip-marker"}).json()
    assert body["total"] == 1
    assert body["items"][0]["model"] == "claude-sonnet-5"
    assert body["items"][0]["input_preview"] == "roundtrip-marker"


def test_dashboard_page_served(client_and_engine):
    # Serves the built React dashboard when present, else the legacy HTML page;
    # either way it's an HTML document. The UI itself is verified in the browser.
    client, _ = client_and_engine
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_vendored_chartjs_served(client_and_engine):
    client, _ = client_and_engine
    resp = client.get("/static/chart.umd.min.js")
    assert resp.status_code == 200
    assert "Chart" in resp.text


def test_logs_text_search(client_and_engine):
    client, engine = client_and_engine
    _add(engine, input_preview="tell me about lighthouses", output_preview="a lighthouse is...")
    _add(engine, input_preview="capital of France", output_preview="Paris")
    _add(engine, status="error", error_type="RateLimit", error_message="429 quota exceeded")

    assert client.get("/logs", params={"q": "lighthouse"}).json()["total"] == 1
    assert client.get("/logs", params={"q": "PARIS"}).json()["total"] == 1  # case-insensitive
    assert client.get("/logs", params={"q": "quota"}).json()["total"] == 1  # matches error_message
