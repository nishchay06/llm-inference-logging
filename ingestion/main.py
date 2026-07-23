from fastapi import FastAPI

from sdk.events import InferenceLog

# A SEPARATE service from the chatbot. It receives inference logs over HTTP.
# For now it just validates and prints; Rung 6 will store them in a database.
app = FastAPI(title="Ingestion — Rung 4")


@app.get("/hello")
def hello():
    return {"message": "ingestion is up"}


@app.post("/logs")
def ingest(event: InferenceLog):
    # FastAPI validates the incoming JSON against InferenceLog automatically —
    # the SAME schema the SDK used to build it. That shared model IS the
    # contract between the two services. A malformed payload never reaches this
    # body: it gets a 422 first (the Rung 0 lesson, now across a network).
    print("==== ingested inference log ====")
    print(event.model_dump_json(indent=2))
    return {"status": "received", "event_id": event.event_id}
