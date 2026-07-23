import uuid

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

# Load variables from a local .env file into the process environment, so
# ANTHROPIC_API_KEY becomes visible to the Anthropic client below.
load_dotenv()

app = FastAPI(title="Chatbot — Rung 2")

# One shared client for the whole app. With no arguments it reads
# ANTHROPIC_API_KEY from the environment automatically.
client = Anthropic()
MODEL = "claude-sonnet-5"

# How many of the most recent messages we send to the model each turn. This is
# the "short conversational context": we deliberately do NOT resend the whole
# history forever, or every call would grow without bound — more tokens means
# slower and more expensive requests.
MAX_CONTEXT_MESSAGES = 10

# In-memory conversation store: session_id -> list of messages.
# This lives in the process's RAM, so it is LOST when the server restarts and
# is not shared across multiple server processes. That's fine for now; Rung 6
# moves conversations into a database. (A tradeoff worth noting in the README.)
CONVERSATIONS: dict[str, list[dict]] = {}


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str
    # The client passes this back to continue an existing conversation. On the
    # first message it is omitted, and we mint a new one.
    session_id: str | None = None


def context_window(history: list[dict]) -> list[dict]:
    """The most recent messages we actually send to the model.

    Take the last MAX_CONTEXT_MESSAGES, then trim any leading assistant turns
    so the window starts on a user message (the API requires messages[0] to be
    role 'user').
    """
    window = history[-MAX_CONTEXT_MESSAGES:]
    while window and window[0]["role"] != "user":
        window = window[1:]
    return window


@app.post("/chat")
def chat(payload: ChatRequest):
    # Find an existing conversation, or start a fresh one.
    session_id = payload.session_id or str(uuid.uuid4())
    history = CONVERSATIONS.setdefault(session_id, [])

    # Record the new user message in our own store.
    history.append({"role": "user", "content": payload.message})

    # Send the recent history — NOT just the new message. This is what gives the
    # bot "memory": the model is stateless, so memory is something WE rebuild
    # and resend on every turn.
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=context_window(history),
    )
    reply = next((b.text for b in response.content if b.type == "text"), "")

    # Record the assistant's reply so the next turn can see it too.
    history.append({"role": "assistant", "content": reply})

    print("---- inference metadata (a peek ahead to Rung 3) ----")
    print("session_id:   ", session_id)
    print("model:        ", response.model)
    print("stop_reason:  ", response.stop_reason)
    print("input_tokens: ", response.usage.input_tokens)
    print("output_tokens:", response.usage.output_tokens)
    print("history_len:  ", len(history))

    return {"reply": reply, "session_id": session_id}
