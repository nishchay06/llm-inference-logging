import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from db.engine import engine, get_session
from db.models import Conversation, Message
from sdk.providers import DEFAULT_MODELS, build_client
from sdk.sinks import HttpSink, QueueSink
from sdk.tracing import TracedClient

load_dotenv()

# Configuring logging is the application's job, not the library's: modules under
# sdk/ only ever call getLogger(__name__), so without a root handler here their
# records (e.g. a dropped inference log) would be silently discarded. Uvicorn
# configures only its own loggers, so this is what makes ours visible.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Schema is created out-of-band by `python -m db.init` (see db/init.py), not on
# startup — two services racing to CREATE TABLE on boot causes a concurrent-DDL
# error, and schema management belongs in a migration step anyway.
app = FastAPI(title="Chatbot")

INGESTION_URL = os.getenv("INGESTION_URL", "http://127.0.0.1:8001/logs")
REDIS_URL = os.getenv("REDIS_URL")


def _build_inner_sink():
    """Transport for inference logs: a durable Redis Stream when REDIS_URL is set
    (a worker consumes it — see ingestion/worker.py), else a direct HTTP POST to
    the ingestion service. Either way it's wrapped in a QueueSink so the chat
    never blocks or breaks on the log path."""
    if REDIS_URL:
        from redis import Redis

        from sdk.sinks import RedisStreamSink

        return RedisStreamSink(Redis.from_url(REDIS_URL))
    return HttpSink(INGESTION_URL)


# Build a wrapped client for every provider whose API key is configured, so the
# UI can switch providers per request. They share one QueueSink (one background
# shipper). A provider with no key is simply skipped, not offered.
_sink = QueueSink(_build_inner_sink())
TRACED: dict[str, TracedClient] = {}
MODEL_FOR: dict[str, str] = {}
for _provider in DEFAULT_MODELS:
    try:
        TRACED[_provider] = TracedClient(
            build_client(_provider), provider=_provider, sink=_sink
        )
        MODEL_FOR[_provider] = DEFAULT_MODELS[_provider]
    except Exception:
        # provider not configured (e.g. missing key) — leave it out
        pass

# Default provider from env, falling back to whatever is available. LLM_MODEL (if
# set) overrides the model for the default provider only.
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
if DEFAULT_PROVIDER not in TRACED:
    DEFAULT_PROVIDER = next(iter(TRACED), "")
if os.getenv("LLM_MODEL") and DEFAULT_PROVIDER in MODEL_FOR:
    MODEL_FOR[DEFAULT_PROVIDER] = os.environ["LLM_MODEL"]

MAX_CONTEXT_MESSAGES = 10


STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
# The built React app (frontend/dist) — produced by `npm run build` (locally, or
# the Docker multi-stage build). Served at "/" when present; see end of file.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

# Legacy static assets (the old plain-HTML UI's marked.min.js) served under
# /static; kept as the fallback when the React build isn't present.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    provider: str | None = None  # optional; defaults to DEFAULT_PROVIDER


class ProviderInfo(BaseModel):
    name: str
    model: str


class ProvidersOut(BaseModel):
    providers: list[ProviderInfo]
    default: str


@app.get("/providers", response_model=ProvidersOut)
def providers():
    """Which providers the UI can offer (only those with a key configured)."""
    return ProvidersOut(
        providers=[ProviderInfo(name=p, model=MODEL_FOR[p]) for p in TRACED],
        default=DEFAULT_PROVIDER,
    )


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


def _resolve_provider(payload: ChatRequest):
    """Pick the provider (default if unspecified); 400 if it isn't available.
    Runs before any DB write or LLM call so failures are fast and clean."""
    provider = payload.provider or DEFAULT_PROVIDER
    if provider not in TRACED:
        raise HTTPException(status_code=400, detail=f"provider {provider!r} not available")
    return provider, TRACED[provider], MODEL_FOR[provider]


def _record_user_message(session_id: str, message: str) -> list[dict]:
    """Upsert the conversation, store the user message, and return the context
    window (last N messages). History lives in Postgres, not process RAM."""
    with Session(engine) as db:
        if db.get(Conversation, session_id) is None:
            db.add(Conversation(session_id=session_id))
        db.add(Message(session_id=session_id, role="user", content=message))
        db.commit()
        recent = db.exec(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(MAX_CONTEXT_MESSAGES)
        ).all()
        return _trim_to_user_start(
            [{"role": m.role, "content": m.content} for m in reversed(recent)]
        )


def _record_assistant_message(session_id: str, content: str) -> None:
    """Store the assistant reply (full, or partial on cancel) and bump the
    conversation's updated_at."""
    with Session(engine) as db:
        db.add(Message(session_id=session_id, role="assistant", content=content))
        conv = db.get(Conversation, session_id)
        if conv is not None:
            conv.updated_at = datetime.now(timezone.utc)
            db.add(conv)
        db.commit()


@app.post("/chat")
def chat(payload: ChatRequest):
    _, traced, model = _resolve_provider(payload)
    session_id = payload.session_id or str(uuid.uuid4())

    window = _record_user_message(session_id, payload.message)
    reply = traced.chat(
        model=model, max_tokens=1024, messages=window, session_id=session_id
    ).text
    _record_assistant_message(session_id, reply)

    return {"reply": reply, "session_id": session_id}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/chat/stream")
def chat_stream(payload: ChatRequest):
    provider, traced, model = _resolve_provider(payload)
    session_id = payload.session_id or str(uuid.uuid4())
    window = _record_user_message(session_id, payload.message)

    def event_stream():
        parts: list[str] = []
        stream_gen = traced.stream(
            model=model, max_tokens=1024, messages=window, session_id=session_id
        )
        try:
            yield _sse({"type": "start", "session_id": session_id, "provider": provider, "model": model})
            for delta in stream_gen:
                parts.append(delta)
                yield _sse({"type": "delta", "text": delta})
            yield _sse({"type": "done", "session_id": session_id})
        except Exception as exc:  # provider/stream error mid-flight
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            # Close the wrapper (emits the success/cancelled/error log) and
            # persist whatever text was produced — full, or partial on cancel.
            stream_gen.close()
            if parts:
                _record_assistant_message(session_id, "".join(parts))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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


# Serve the frontend at "/", mounted LAST so every API route above takes
# precedence. The built React app when present (production / Docker), else the
# legacy plain-HTML page (a `uvicorn` run without `npm run build` — use
# `npm run dev` for local frontend work).
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
else:

    @app.get("/")
    def index():
        return FileResponse(INDEX_HTML)
