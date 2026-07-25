"""Guards on the SQLite-vs-Postgres split.

`/stats` and `/stats/timeseries` branch on the SQL dialect — ordered-set
aggregates and epoch bucketing on Postgres, a Python fallback on SQLite. Two
things can go wrong with that arrangement, and each gets a test here:

1. A CI job that *believes* it is testing Postgres but quietly runs SQLite would
   pass while covering nothing. `test_dialect_is_the_configured_one` makes that
   impossible to miss.
2. The two branches could drift and answer the same question differently.
   `test_percentile_definition_is_backend_independent` pins the definition, and
   passes on both backends by construction.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import RUNNING_ON_POSTGRES, make_engine
from db.engine import get_session
from db.models import InferenceLogRow
from ingestion.main import app


@pytest.fixture
def client_and_engine():
    engine = make_engine()

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app), engine
    app.dependency_overrides.clear()


def _add(engine, started_at, latency_ms, status="success"):
    with Session(engine) as s:
        s.add(
            InferenceLogRow(
                provider="anthropic",
                model="claude-sonnet-5",
                status=status,
                started_at=started_at,
                ended_at=started_at,
                latency_ms=latency_ms,
            )
        )
        s.commit()


def test_dialect_is_the_configured_one(client_and_engine):
    """TEST_DATABASE_URL set means the Postgres branches must actually be running.
    Without this, a broken CI service container would produce a green SQLite run
    labelled 'postgres'."""
    _, engine = client_and_engine
    expected = "postgresql" if RUNNING_ON_POSTGRES else "sqlite"
    assert engine.dialect.name == expected


def test_percentile_definition_is_backend_independent(client_and_engine):
    """Nearest-rank, returning an actually observed value — `percentile_disc` on
    Postgres, the Python path on SQLite. Both must agree on these numbers."""
    client, engine = client_and_engine
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for ms in (10.0, 20.0, 30.0, 40.0, 100.0):
        _add(engine, t, ms)

    body = client.get("/stats").json()
    assert body["p50_ms"] == 30.0
    assert body["p95_ms"] == 100.0
    assert body["p99_ms"] == 100.0
    # An observed value, never an interpolated one (percentile_cont would give 34.0)
    assert body["p50_ms"] in (10.0, 20.0, 30.0, 40.0, 100.0)


def test_timeseries_bucketing_is_backend_independent(client_and_engine):
    """Bucket boundaries must land identically whether Postgres floors the epoch
    in SQL or Python does it in a dict."""
    client, engine = client_and_engine
    t = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)
    _add(engine, t + timedelta(seconds=5), 100.0)
    _add(engine, t + timedelta(seconds=55), 300.0)
    _add(engine, t + timedelta(seconds=70), 500.0, status="error")

    points = client.get("/stats/timeseries", params={"bucket": 60}).json()["points"]
    assert len(points) == 2

    first, second = points
    assert first["start"].startswith("2026-03-05T12:00:00")
    assert (first["calls"], first["errors"]) == (2, 0)
    assert first["avg_latency_ms"] == 200.0

    assert second["start"].startswith("2026-03-05T12:01:00")
    assert (second["calls"], second["errors"]) == (1, 1)


def test_aware_timestamps_are_stored_as_utc_not_server_local(client_and_engine):
    """Regression: the timestamp columns are naive, and Postgres converts an
    *aware* datetime to the session TimeZone before discarding the offset. Writing
    the SDK's aware UTC stamps to a server running in, say, Asia/Kolkata therefore
    used to store them +5:30 off — silently, and only on non-UTC servers.

    `UtcDateTime` normalises on bind, so the round-tripped value must equal the
    UTC wall clock that went in, on any server timezone and either backend.
    """
    _, engine = client_and_engine
    aware = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)
    _add(engine, aware, 100.0)

    with Session(engine) as s:
        stored = s.exec(select(InferenceLogRow)).one().started_at

    # Stored naive, and equal to the UTC wall clock — not shifted by the offset.
    assert stored.replace(tzinfo=None) == datetime(2026, 3, 5, 12, 0, 0)


def test_since_filter_agrees_with_stored_timestamps(client_and_engine):
    """The `since` filter normalises an aware bound the same way writes do, so a
    row written at T is included by since=T and excluded by since=T+1s. A
    mismatch between the two conversions would silently drop rows from every
    dashboard window."""
    client, engine = client_and_engine
    t = datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc)
    _add(engine, t, 100.0)

    assert client.get("/stats", params={"since": t.isoformat()}).json()["total_calls"] == 1
    later = (t + timedelta(seconds=1)).isoformat()
    assert client.get("/stats", params={"since": later}).json()["total_calls"] == 0


def test_empty_window_is_null_not_zero(client_and_engine):
    """An empty window has no latency, which is null — not 0.0. Easy to get wrong
    differently on each backend, since SQL AVG and Python both need handling."""
    client, _ = client_and_engine
    body = client.get("/stats").json()
    assert body["total_calls"] == 0
    assert body["avg_latency_ms"] is None
    assert body["p50_ms"] is None
    assert body["error_rate"] == 0.0
