import uuid

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from sdk.sinks import emit
from sdk.tracing import TracedClient

# Load variables from a local .env file into the process environment, so
# ANTHROPIC_API_KEY becomes visible to the Anthropic client below.
load_dotenv()

app = FastAPI(title="Chatbot — Rung 3")

# The raw provider client, wrapped so every call is instrumented. The chat code
# below talks to `traced`, never to the raw client — so it captures nothing and
# cares about nothing to do with logging.
client = Anthropic()
traced = TracedClient(client, provider="anthropic", sink=emit)

MODEL = "claude-sonnet-5"
MAX_CONTEXT_MESSAGES = 10

# In-memory conversation store: session_id -> list of messages.
# Lost on restart, not shared across processes. Rung 6 moves this to a database.
CONVERSATIONS: dict[str, list[dict]] = {}


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def context_window(history: list[dict]) -> list[dict]:
    """The most recent messages we send to the model: the last
    MAX_CONTEXT_MESSAGES, trimmed to start on a user turn."""
    window = history[-MAX_CONTEXT_MESSAGES:]
    while window and window[0]["role"] != "user":
        window = window[1:]
    return window


@app.post("/chat")
def chat(payload: ChatRequest):
    session_id = payload.session_id or str(uuid.uuid4())
    history = CONVERSATIONS.setdefault(session_id, [])

    history.append({"role": "user", "content": payload.message})

    # One call. All the timing, token extraction, and metadata capture happens
    # inside the wrapper — this function stays pure chat logic.
    response = traced.chat(
        model=MODEL,
        max_tokens=1024,
        messages=context_window(history),
        session_id=session_id,
    )
    reply = next((b.text for b in response.content if b.type == "text"), "")

    history.append({"role": "assistant", "content": reply})

    return {"reply": reply, "session_id": session_id}
