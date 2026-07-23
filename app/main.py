from fastapi import FastAPI
from pydantic import BaseModel

# The application object. Every route attaches to this. uvicorn looks for it
# by the import path "app.main:app" when it starts the server.
app = FastAPI(title="Chatbot — Rung 0")


@app.get("/hello")
def hello():
    # A GET with no input. FastAPI serializes the returned dict to JSON.
    return {"message": "hello, world"}


class EchoRequest(BaseModel):
    # A Pydantic model = the schema for the request body. FastAPI validates
    # incoming JSON against this *before* our function runs. Wrong shape ->
    # automatic 422 response, our code never executes.
    text: str


@app.post("/echo")
def echo(payload: EchoRequest):
    # Because the parameter is typed as EchoRequest, FastAPI knows the body
    # must match that schema. `payload` is a validated Python object here.
    return {"you_sent": payload.text, "length": len(payload.text)}
