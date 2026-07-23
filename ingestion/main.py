from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlmodel import Session, select

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


class StatsOut(BaseModel):
    since: datetime | None
    total_calls: int
    success_count: int
    error_count: int
    error_rate: float
    avg_latency_ms: float | None
    total_input_tokens: int
    total_output_tokens: int


@app.get("/stats", response_model=StatsOut)
def stats(since: datetime | None = None, session: Session = Depends(get_session)):
    """Latency / throughput / errors over the inference logs — the dashboard
    seed. `since` (ISO 8601) optionally windows the query; `started_at` is
    indexed for it. Single aggregate read, all computed in the database."""
    # started_at is stored tz-naive (UTC); normalise an incoming aware value so
    # the comparison is apples-to-apples on Postgres too.
    if since is not None and since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)

    is_error = case((InferenceLogRow.status != "success", 1), else_=0)
    query = select(
        func.count(),
        func.sum(case((InferenceLogRow.status == "success", 1), else_=0)),
        func.sum(is_error),
        func.avg(InferenceLogRow.latency_ms),
        func.sum(InferenceLogRow.input_tokens),
        func.sum(InferenceLogRow.output_tokens),
    )
    if since is not None:
        query = query.where(InferenceLogRow.started_at >= since)

    total, success, errors, avg_latency, itok, otok = session.exec(query).one()
    total = total or 0
    errors = errors or 0
    return StatsOut(
        since=since,
        total_calls=total,
        success_count=success or 0,
        error_count=errors,
        error_rate=(errors / total) if total else 0.0,
        avg_latency_ms=avg_latency,  # None when the window is empty
        total_input_tokens=itok or 0,
        total_output_tokens=otok or 0,
    )
