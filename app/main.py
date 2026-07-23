import os

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

# Load variables from a local .env file into the process environment, so
# ANTHROPIC_API_KEY becomes visible to the Anthropic client below.
load_dotenv()

app = FastAPI(title="Chatbot — Rung 1")

# One shared client for the whole app. With no arguments it reads
# ANTHROPIC_API_KEY from the environment automatically — we never hardcode
# the key into the source.
client = Anthropic()

# The model id is a plain string. Swapping it (e.g. to "claude-sonnet-5" or
# "claude-haiku-4-5") is a one-line change — and "model" is one of the very
# metadata fields we'll log in Rung 3.
MODEL = "claude-sonnet-5"


@app.get("/hello")
def hello():
    return {"message": "hello, world"}


class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
def chat(payload: ChatRequest):
    # The LLM call is just one HTTP request. We send a list of messages and
    # get back a response object. The API is STATELESS: it remembers nothing
    # between calls, so each request must carry whatever context we want the
    # model to see. Here we send only the single new message — no memory yet
    # (that's Rung 2).
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": payload.message}],
    )

    # response.content is a LIST of content blocks, not a plain string. Pull
    # the text out of the first "text" block.
    reply = next((b.text for b in response.content if b.type == "text"), "")

    # Everything Rung 3 will want to capture already lives on this response
    # object. Print it now so you can SEE where each field comes from.
    print("---- inference metadata (a peek ahead to Rung 3) ----")
    print("model:        ", response.model)
    print("stop_reason:  ", response.stop_reason)
    print("input_tokens: ", response.usage.input_tokens)
    print("output_tokens:", response.usage.output_tokens)

    return {"reply": reply}
