import os
import uuid
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from sqlmodel import Session, select

from db.engine import engine
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


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


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
