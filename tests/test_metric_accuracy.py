"""Cancellation must not be counted as failure or as latency.

Two defects found by looking at real dashboard numbers rather than at tests: with
36 successful calls, 0 errors and 2 user cancellations, the console reported a
5.3% error rate and a p99 of 280 seconds.

Both come from treating `cancelled` as "not success":

1. **Error rate.** A user pressing Cancel is not a service failure. Counting it as
   one makes the error-rate KPI — the number an operator reacts to — wrong.

2. **Latency.** A cancelled stream's `latency_ms` runs until the generator is
   finalized, not until the user left, so it is unbounded and arbitrary. Feeding
   it into avg/p50/p95/p99 destroyed those metrics: p99 read 280,004 ms where the
   real figure across successful calls was 8,202 ms.

Errors are deliberately *kept* in the latency aggregates: a call that failed after
a five-second timeout is a genuine latency observation, and dropping it would hide
exactly the slowness worth alerting on. Only `cancelled` is unbounded.

These run on both SQLite and Postgres, so they also pin the dialect-specific
percentile path.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import make_engine
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


def _add(engine, status="success", latency_ms=100.0, model="claude-sonnet-5",
         provider="anthropic", started_at=None):
    with Session(engine) as s:
        s.add(
            InferenceLogRow(
                session_id="s",
                provider=provider,
                model=model,
                status=status,
                error_type="X" if status == "error" else None,
                started_at=started_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=started_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
                latency_ms=latency_ms,
            )
        )
        s.commit()


# ── error rate ───────────────────────────────────────────────────────────────

def test_cancelled_calls_are_not_errors(client_and_engine):
    """The exact shape of the live data that exposed this: no failures at all,
    but two cancellations."""
    client, engine = client_and_engine
    for _ in range(36):
        _add(engine, status="success")
    for _ in range(2):
        _add(engine, status="cancelled", latency_ms=280_004.0)

    body = client.get("/stats").json()
    assert body["total_calls"] == 38  # cancellations still happened
    assert body["success_count"] == 36
    assert body["error_count"] == 0  # ...but nothing failed
    assert body["error_rate"] == 0.0
    assert body["cancelled_count"] == 2


def test_real_errors_are_still_counted(client_and_engine):
    client, engine = client_and_engine
    _add(engine, status="success")
    _add(engine, status="error")
    _add(engine, status="cancelled")

    body = client.get("/stats").json()
    assert body["total_calls"] == 3
    assert body["error_count"] == 1
    assert body["cancelled_count"] == 1
    assert body["error_rate"] == pytest.approx(1 / 3)


# ── latency ──────────────────────────────────────────────────────────────────

def test_cancelled_latency_is_excluded_from_aggregates(client_and_engine):
    """A cancelled stream's duration is measured until generator finalization, so
    it is arbitrary. It must not reach avg or the percentiles."""
    client, engine = client_and_engine
    for ms in (10.0, 20.0, 30.0, 40.0, 100.0):
        _add(engine, status="success", latency_ms=ms)
    _add(engine, status="cancelled", latency_ms=280_004.0)

    body = client.get("/stats").json()
    assert body["total_calls"] == 6  # still counted as a call
    assert body["avg_latency_ms"] == pytest.approx(40.0)  # mean of the five
    assert body["p50_ms"] == 30.0
    assert body["p95_ms"] == 100.0
    assert body["p99_ms"] == 100.0  # not 280004


def test_error_latency_is_kept(client_and_engine):
    """A timeout is a real latency observation — dropping it would hide slowness."""
    client, engine = client_and_engine
    _add(engine, status="success", latency_ms=100.0)
    _add(engine, status="error", latency_ms=5000.0)

    body = client.get("/stats").json()
    assert body["avg_latency_ms"] == pytest.approx(2550.0)
    assert body["p95_ms"] == 5000.0


def test_latency_is_null_when_only_cancelled_calls_exist(client_and_engine):
    """No usable latency observation — null, not zero and not the bogus figure."""
    client, engine = client_and_engine
    _add(engine, status="cancelled", latency_ms=280_004.0)

    body = client.get("/stats").json()
    assert body["total_calls"] == 1
    assert body["avg_latency_ms"] is None
    assert body["p50_ms"] is None


def test_explicitly_filtering_to_cancelled_still_reports_it(client_and_engine):
    """Excluding cancellations from aggregates must not make them unreadable —
    the log explorer's Cancelled filter has to keep working."""
    client, engine = client_and_engine
    _add(engine, status="success", latency_ms=100.0)
    _add(engine, status="cancelled", latency_ms=280_004.0)

    body = client.get("/stats", params={"status": "cancelled"}).json()
    assert body["total_calls"] == 1
    assert body["cancelled_count"] == 1
    assert client.get("/logs", params={"status": "cancelled"}).json()["total"] == 1


# ── the other two metric endpoints ───────────────────────────────────────────

def test_timeseries_separates_cancelled_from_errors_and_latency(client_and_engine):
    client, engine = client_and_engine
    t = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    _add(engine, status="success", latency_ms=100.0, started_at=t)
    _add(engine, status="error", latency_ms=300.0, started_at=t)
    _add(engine, status="cancelled", latency_ms=280_004.0, started_at=t)

    (point,) = client.get("/stats/timeseries", params={"bucket": 60}).json()["points"]
    assert point["calls"] == 3  # all three happened
    assert point["errors"] == 1  # only the real failure
    assert point["avg_latency_ms"] == pytest.approx(200.0)  # success + error only


def test_by_model_separates_cancelled_from_errors_and_latency(client_and_engine):
    client, engine = client_and_engine
    _add(engine, status="success", latency_ms=100.0)
    _add(engine, status="cancelled", latency_ms=280_004.0)

    (item,) = client.get("/stats/by_model").json()["items"]
    assert item["calls"] == 2
    assert item["error_rate"] == 0.0
    assert item["avg_latency_ms"] == pytest.approx(100.0)
