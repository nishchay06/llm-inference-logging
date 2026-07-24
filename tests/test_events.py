"""Tests for the event-based path: the RedisStreamSink producer, the shared
store_log, and the worker's entry handler. Fakes only — no Redis server, no
network.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from db.models import InferenceLogRow
from ingestion.store import store_log
from ingestion.worker import PoisonMessage, handle_entry
from sdk.events import InferenceLog
from sdk.sinks import RedisStreamSink


def _event(**over):
    base = dict(
        session_id="s1",
        provider="anthropic",
        model="claude-sonnet-5",
        status="success",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        latency_ms=123.4,
        input_tokens=7,
        output_tokens=3,
    )
    base.update(over)
    return InferenceLog(**base)


class _FakeRedis:
    def __init__(self):
        self.added = []

    def xadd(self, stream, fields):
        self.added.append((stream, fields))
        return b"1-0"


def _sqlite_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


# ── producer ─────────────────────────────────────────────────────────────────

def test_redis_stream_sink_xadds_serialized_event():
    r = _FakeRedis()
    sink = RedisStreamSink(r, stream="inference_logs")
    ev = _event(model="claude-sonnet-5")
    sink(ev)

    assert len(r.added) == 1
    stream, fields = r.added[0]
    assert stream == "inference_logs"
    # the event is carried as JSON under "data", round-trips back to the model
    round_tripped = InferenceLog.model_validate_json(fields["data"])
    assert round_tripped.model == "claude-sonnet-5"
    assert round_tripped.event_id == ev.event_id


# ── shared store ─────────────────────────────────────────────────────────────

def test_store_log_persists_row():
    with _sqlite_session() as s:
        store_log(_event(model="gemini-3.6-flash"), s)
        rows = s.exec(select(InferenceLogRow)).all()
    assert len(rows) == 1 and rows[0].model == "gemini-3.6-flash"


# ── consumer (worker entry handler) ──────────────────────────────────────────

def test_worker_handle_entry_stores_good_message():
    ev = _event(status="error", error_type="X", error_message="boom")
    with _sqlite_session() as s:
        handle_entry({"data": ev.model_dump_json()}, s)
        rows = s.exec(select(InferenceLogRow)).all()
    assert len(rows) == 1 and rows[0].status == "error"


def test_worker_handle_entry_poison_raises():
    with _sqlite_session() as s:
        with pytest.raises(PoisonMessage):
            handle_entry({"data": "not-valid-json"}, s)
