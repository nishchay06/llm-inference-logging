"""Tests for the database models, on in-memory SQLite (fast, no server)."""

from datetime import datetime, timezone

from sqlmodel import Session, select

from db.models import Conversation, InferenceLogRow, Message

from conftest import make_engine


def _sqlite_engine():
    return make_engine()


def test_conversation_and_messages_roundtrip():
    engine = _sqlite_engine()
    with Session(engine) as s:
        s.add(Conversation(session_id="abc"))
        s.add(Message(session_id="abc", role="user", content="hi"))
        s.add(Message(session_id="abc", role="assistant", content="hello"))
        s.commit()

    with Session(engine) as s:
        msgs = s.exec(
            select(Message).where(Message.session_id == "abc").order_by(Message.id)
        ).all()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hi", "hello"]


def test_inference_log_row_roundtrip():
    engine = _sqlite_engine()
    with Session(engine) as s:
        s.add(
            InferenceLogRow(
                session_id="abc",
                provider="anthropic",
                model="claude-sonnet-5",
                status="success",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                latency_ms=100.0,
                input_tokens=5,
                output_tokens=3,
            )
        )
        s.commit()

    with Session(engine) as s:
        rows = s.exec(select(InferenceLogRow)).all()
    assert len(rows) == 1
    assert rows[0].model == "claude-sonnet-5"
    assert rows[0].event_id  # auto-generated uuid
