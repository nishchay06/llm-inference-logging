import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from db.engine import engine, get_session
from db.models import Conversation, Message
from sdk.sinks import HttpSink, QueueSink
from sdk.tracing import TracedClient

load_dotenv()

# Schema is created out-of-band by `python -m db.init` (see db/init.py), not on
# startup — two services racing to CREATE TABLE on boot causes a concurrent-DDL
# error, and schema management belongs in a migration step anyway.
app = FastAPI(title="Chatbot — Rung 6")

INGESTION_URL = os.getenv("INGESTION_URL", "http://127.0.0.1:8001/logs")

# The raw provider client, wrapped so every call is instrumented; the sink
# enqueues each log and a background thread ships it to ingestion.
client = Anthropic()
traced = TracedClient(
    client, provider="anthropic", sink=QueueSink(HttpSink(INGESTION_URL))
)

MODEL = "claude-sonnet-5"
MAX_CONTEXT_MESSAGES = 10


INDEX_HTML = Path(__file__).parent / "static" / "index.html"


@app.get("/")
def index():
    """Serve the single-page UI (chat + list/resume conversations)."""
    return FileResponse(INDEX_HTML)


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


# Read-side wire models — the read contract, kept separate from the SQLModel
# storage tables (same wire-≠-storage discipline as sdk.events.InferenceLog).
class MessageOut(BaseModel):
    role: str
    content: str
    created_at: datetime


class ConversationSummary(BaseModel):
    session_id: str
    preview: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationDetail(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


PREVIEW_CHARS = 80


def _truncate(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def _trim_to_user_start(window: list[dict]) -> list[dict]:
    while window and window[0]["role"] != "user":
        window = window[1:]
    return window


@app.post("/chat")
def chat(payload: ChatRequest):
    session_id = payload.session_id or str(uuid.uuid4())

    with Session(engine) as db:
        # Upsert the conversation and record the user's message. Conversation
        # history now lives in Postgres, not process RAM (Rung 2's in-memory
        # dict is retired — it survives restarts and is shared across processes).
        if db.get(Conversation, session_id) is None:
            db.add(Conversation(session_id=session_id))
        db.add(Message(session_id=session_id, role="user", content=payload.message))
        db.commit()

        # Build the context window from the last N messages for this session.
        recent = db.exec(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(MAX_CONTEXT_MESSAGES)
        ).all()
        window = _trim_to_user_start(
            [{"role": m.role, "content": m.content} for m in reversed(recent)]
        )

        response = traced.chat(
            model=MODEL, max_tokens=1024, messages=window, session_id=session_id
        )
        reply = next((b.text for b in response.content if b.type == "text"), "")

        db.add(Message(session_id=session_id, role="assistant", content=reply))
        conv = db.get(Conversation, session_id)
        conv.updated_at = datetime.now(timezone.utc)
        db.add(conv)
        db.commit()

    return {"reply": reply, "session_id": session_id}


@app.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(db: Session = Depends(get_session)):
    """List conversations, most-recently-active first.

    Three aggregate reads, NOT N+1: the conversations (ordered), a grouped
    message count per session, and the first *user* message per session for the
    preview (a group-wise-first query). Stitched together in Python.
    """
    convs = db.exec(select(Conversation).order_by(Conversation.updated_at.desc())).all()

    counts = dict(
        db.exec(
            select(Message.session_id, func.count()).group_by(Message.session_id)
        ).all()
    )

    # id of the first user message per session, then fetch just those rows.
    first_ids = db.exec(
        select(Message.session_id, func.min(Message.id))
        .where(Message.role == "user")
        .group_by(Message.session_id)
    ).all()
    id_by_session = {sid: mid for sid, mid in first_ids}
    preview_by_session: dict[str, str] = {}
    if id_by_session:
        first_msgs = db.exec(
            select(Message).where(Message.id.in_(list(id_by_session.values())))
        ).all()
        content_by_id = {m.id: m.content for m in first_msgs}
        preview_by_session = {
            sid: content_by_id[mid] for sid, mid in id_by_session.items()
        }

    return [
        ConversationSummary(
            session_id=c.session_id,
            preview=_truncate(preview_by_session.get(c.session_id, "")),
            message_count=counts.get(c.session_id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in convs
    ]


@app.get("/conversations/{session_id}", response_model=ConversationDetail)
def get_conversation(session_id: str, db: Session = Depends(get_session)):
    """Resume a conversation: its full message history in order."""
    conv = db.get(Conversation, session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    msgs = db.exec(
        select(Message).where(Message.session_id == session_id).order_by(Message.id)
    ).all()
    return ConversationDetail(
        session_id=conv.session_id,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=[
            MessageOut(role=m.role, content=m.content, created_at=m.created_at)
            for m in msgs
        ],
    )
