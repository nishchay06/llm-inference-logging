from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from sqlmodel import Session

from db.engine import get_session
from db.models import InferenceLogRow
from sdk.events import InferenceLog

load_dotenv()

# Schema is created out-of-band by `python -m db.init` (see db/init.py), not on
# startup — see the note in app/main.py.
app = FastAPI(title="Ingestion — Rung 6")


@app.get("/hello")
def hello():
    return {"message": "ingestion is up"}


@app.post("/logs")
def ingest(event: InferenceLog, session: Session = Depends(get_session)):
    # FastAPI validated the incoming JSON against InferenceLog (the wire model);
    # we map it to InferenceLogRow (the storage model) and insert.
    session.add(InferenceLogRow(**event.model_dump()))
    session.commit()
    return {"status": "stored", "event_id": event.event_id}
