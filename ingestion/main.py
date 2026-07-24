import math
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlmodel import Session, select

from db.engine import get_session
from db.models import InferenceLogRow
from sdk.events import InferenceLog

load_dotenv()

# Schema is created out-of-band by `python -m db.init` (see db/init.py), not on
# startup — see the note in app/main.py.
app = FastAPI(title="Ingestion — Rung 6")

# The observability console — ingestion owns the telemetry, so it serves the
# dashboard (reads inference_logs via the /stats* and /logs endpoints below).
STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/dashboard")
def dashboard():
    return FileResponse(DASHBOARD_HTML)


def _naive_utc(since: datetime | None) -> datetime | None:
    """started_at is stored tz-naive (UTC); normalise an aware `since` so the
    comparison is apples-to-apples on Postgres too."""
    if since is not None and since.tzinfo is not None:
        return since.astimezone(timezone.utc).replace(tzinfo=None)
    return since


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Computed in Python so it works on both Postgres
    and the SQLite used in tests (percentile_cont is Postgres-only)."""
    if not sorted_vals:
        return None
    k = math.ceil(p / 100 * len(sorted_vals)) - 1
    return sorted_vals[min(len(sorted_vals) - 1, max(0, k))]


def _log_conditions(
    *, status=None, provider=None, model=None, session_id=None, q=None, since=None
):
    """Shared WHERE conditions for the log query + all the metrics endpoints, so
    the dashboard's filter bar scopes BOTH planes (charts and the stream)
    identically. `since` must already be naive-UTC (see _naive_utc)."""
    conds = []
    if status:
        conds.append(InferenceLogRow.status == status)
    if provider:
        conds.append(InferenceLogRow.provider == provider)
    if model:
        conds.append(InferenceLogRow.model == model)
    if session_id:
        conds.append(InferenceLogRow.session_id == session_id)
    if since is not None:
        conds.append(InferenceLogRow.started_at >= since)
    if q:
        like = f"%{q}%"
        conds.append(
            or_(
                InferenceLogRow.input_preview.ilike(like),
                InferenceLogRow.output_preview.ilike(like),
                InferenceLogRow.error_message.ilike(like),
            )
        )
    return conds


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
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    total_input_tokens: int
    total_output_tokens: int


@app.get("/stats", response_model=StatsOut)
def stats(
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    session: Session = Depends(get_session),
):
    """Latency / throughput / errors over the (filtered) inference logs. Counts/avg
    come from a SQL aggregate; latency percentiles are computed in Python. The
    same filters as /logs apply, so the dashboard's filter bar scopes this too."""
    since = _naive_utc(since)
    conds = _log_conditions(status=status, provider=provider, model=model, q=q, since=since)

    query = select(
        func.count(),
        func.sum(case((InferenceLogRow.status == "success", 1), else_=0)),
        func.sum(case((InferenceLogRow.status != "success", 1), else_=0)),
        func.avg(InferenceLogRow.latency_ms),
        func.sum(InferenceLogRow.input_tokens),
        func.sum(InferenceLogRow.output_tokens),
    )
    lat_query = select(InferenceLogRow.latency_ms)
    for c in conds:
        query = query.where(c)
        lat_query = lat_query.where(c)

    total, success, errors, avg_latency, itok, otok = session.exec(query).one()
    total = total or 0
    errors = errors or 0
    latencies = sorted(v for v in session.exec(lat_query).all() if v is not None)
    return StatsOut(
        since=since,
        total_calls=total,
        success_count=success or 0,
        error_count=errors,
        error_rate=(errors / total) if total else 0.0,
        avg_latency_ms=avg_latency,  # None when the window is empty
        p50_ms=_percentile(latencies, 50),
        p95_ms=_percentile(latencies, 95),
        p99_ms=_percentile(latencies, 99),
        total_input_tokens=itok or 0,
        total_output_tokens=otok or 0,
    )


class TimeseriesPoint(BaseModel):
    start: datetime
    calls: int
    errors: int
    avg_latency_ms: float | None


class TimeseriesOut(BaseModel):
    bucket_seconds: int
    points: list[TimeseriesPoint]


@app.get("/stats/timeseries", response_model=TimeseriesOut)
def timeseries(
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    bucket: int = 60,
    session: Session = Depends(get_session),
):
    """Per-bucket volume for the throughput/latency charts. Rows are bucketed in
    Python (date_trunc is Postgres-only); only non-empty buckets are returned."""
    since = _naive_utc(since)
    query = select(
        InferenceLogRow.started_at, InferenceLogRow.status, InferenceLogRow.latency_ms
    )
    for c in _log_conditions(status=status, provider=provider, model=model, q=q, since=since):
        query = query.where(c)

    buckets: dict[int, dict] = {}
    for started_at, status, lat in session.exec(query).all():
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        b = int(started_at.timestamp() // bucket) * bucket
        agg = buckets.setdefault(b, {"calls": 0, "errors": 0, "sum": 0.0, "n": 0})
        agg["calls"] += 1
        if status != "success":
            agg["errors"] += 1
        if lat is not None:
            agg["sum"] += lat
            agg["n"] += 1

    points = [
        TimeseriesPoint(
            start=datetime.fromtimestamp(b, timezone.utc),
            calls=agg["calls"],
            errors=agg["errors"],
            avg_latency_ms=(agg["sum"] / agg["n"]) if agg["n"] else None,
        )
        for b, agg in sorted(buckets.items())
    ]
    return TimeseriesOut(bucket_seconds=bucket, points=points)


class ByModelItem(BaseModel):
    model: str
    provider: str
    calls: int
    error_rate: float
    avg_latency_ms: float | None


class ByModelOut(BaseModel):
    items: list[ByModelItem]


@app.get("/stats/by_model", response_model=ByModelOut)
def by_model(
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    session: Session = Depends(get_session),
):
    """Per-model breakdown (calls, error rate, avg latency) — shows the
    multi-provider mix. Honours the same filters as the rest of the dashboard."""
    since = _naive_utc(since)
    query = select(
        InferenceLogRow.model,
        InferenceLogRow.provider,
        func.count(),
        func.sum(case((InferenceLogRow.status != "success", 1), else_=0)),
        func.avg(InferenceLogRow.latency_ms),
    ).group_by(InferenceLogRow.model, InferenceLogRow.provider)
    for c in _log_conditions(status=status, provider=provider, model=model, q=q, since=since):
        query = query.where(c)

    items = []
    for model, provider, calls, errors, avg_lat in session.exec(query).all():
        errors = errors or 0
        items.append(
            ByModelItem(
                model=model,
                provider=provider,
                calls=calls,
                error_rate=(errors / calls) if calls else 0.0,
                avg_latency_ms=avg_lat,
            )
        )
    return ByModelOut(items=items)


class LogItem(BaseModel):
    event_id: str
    session_id: str | None
    provider: str
    model: str
    status: str
    error_type: str | None
    error_message: str | None
    latency_ms: float
    ttft_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    input_preview: str | None
    output_preview: str | None
    started_at: datetime
    created_at: datetime


class LogsOut(BaseModel):
    total: int
    items: list[LogItem]


@app.get("/logs", response_model=LogsOut)
def query_logs(
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
):
    """The log stream for the explorer: filtered, newest-first, paginated.
    Coexists with POST /logs (write) — GET queries, POST ingests."""
    since = _naive_utc(since)
    conds = _log_conditions(
        status=status, provider=provider, model=model, session_id=session_id, q=q, since=since
    )

    count_q = select(func.count()).select_from(InferenceLogRow)
    rows_q = select(InferenceLogRow)
    for c in conds:
        count_q = count_q.where(c)
        rows_q = rows_q.where(c)

    total = session.exec(count_q).one()
    rows = session.exec(
        rows_q.order_by(InferenceLogRow.started_at.desc()).offset(offset).limit(limit)
    ).all()
    items = [
        LogItem(**{k: getattr(r, k) for k in LogItem.model_fields}) for r in rows
    ]
    return LogsOut(total=total, items=items)
