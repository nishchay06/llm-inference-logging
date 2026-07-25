import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlmodel import Session, select

from db.engine import get_session
from db.models import InferenceLogRow
from ingestion.store import store_log
from sdk.events import InferenceLog

load_dotenv()

# See the note in app/main.py: the application configures logging, the library
# only requests a logger.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Schema is created out-of-band by `python -m db.init` (see db/init.py), not on
# startup — see the note in app/main.py.
app = FastAPI(title="Inference log ingestion")

# Upper bound on a single page of logs (see GET /logs).
MAX_PAGE_SIZE = 500

# The observability console — ingestion owns the telemetry, so it serves the
# dashboard (reads inference_logs via the /stats* and /logs endpoints below).
# Built by `npm run build` or the Docker multi-stage build; served at / and
# /dashboard when present (mounted at end of file so API routes take precedence).
DASHBOARD_DIST = Path(__file__).parent.parent / "dashboard" / "dist"

DASHBOARD_NOT_BUILT = (
    "<h1>Dashboard not built</h1>"
    "<p>Run <code>cd dashboard &amp;&amp; npm install &amp;&amp; npm run build</code>, "
    "or use <code>npm run dev</code> for live reload on :5174. "
    "<code>docker compose up</code> builds it automatically.</p>"
    "<p>The metrics API is unaffected — see <a href='/docs'>/docs</a>.</p>"
)


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """The console. Without a build the metrics endpoints still work, so this
    explains how to build rather than returning a bare 404."""
    index = DASHBOARD_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(DASHBOARD_NOT_BUILT, status_code=503)


def _naive_utc(since: datetime | None) -> datetime | None:
    """started_at is stored tz-naive (UTC); normalise an aware `since` so the
    comparison is apples-to-apples on Postgres too."""
    if since is not None and since.tzinfo is not None:
        return since.astimezone(timezone.utc).replace(tzinfo=None)
    return since


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


# A user pressing Cancel is not a service failure, and it is not a latency
# observation either: a cancelled stream's latency_ms runs until the generator is
# finalized rather than until the user left, so it is unbounded and arbitrary.
# Live data showed two cancellations turning a true 0% error rate into 5.3% and a
# true p99 of 8.2s into 280s. Errors are treated differently on purpose — a call
# that failed after a timeout is a genuine latency observation worth keeping.
IS_ERROR = InferenceLogRow.status == "error"
IS_CANCELLED = InferenceLogRow.status == "cancelled"
HAS_USABLE_LATENCY = InferenceLogRow.status != "cancelled"


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile, in Python — the fallback path for backends with
    no ordered-set aggregates (the SQLite used in tests). See `_percentiles`."""
    if not sorted_vals:
        return None
    k = math.ceil(p / 100 * len(sorted_vals)) - 1
    return sorted_vals[min(len(sorted_vals) - 1, max(0, k))]


def _percentiles(session: Session, conds: list) -> tuple:
    """p50/p95/p99 of latency over the filtered rows.

    On Postgres this is an ordered-set aggregate computed **in the database**, so
    a million-row window transfers three numbers rather than a million. SQLite
    has no `percentile_disc`, so tests fall back to sorting in Python — fine at
    test sizes, and never the production path.

    `percentile_disc` (not `_cont`) is deliberate: it returns an actual observed
    value, matching the nearest-rank definition `_percentile` implements, so both
    backends give identical answers for identical data.

    Cancelled calls are excluded — see HAS_USABLE_LATENCY.
    """
    if _dialect(session) == "postgresql":
        query = select(
            *[
                func.percentile_disc(p).within_group(InferenceLogRow.latency_ms.asc())
                for p in (0.5, 0.95, 0.99)
            ]
        ).where(HAS_USABLE_LATENCY)
        for c in conds:
            query = query.where(c)
        return tuple(session.exec(query).one())

    query = select(InferenceLogRow.latency_ms).where(HAS_USABLE_LATENCY)
    for c in conds:
        query = query.where(c)
    latencies = sorted(v for v in session.exec(query).all() if v is not None)
    return tuple(_percentile(latencies, p) for p in (50, 95, 99))


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
    # store_log maps it to the storage row and inserts — shared with the worker.
    store_log(event, session)
    return {"status": "stored", "event_id": event.event_id}


class StatsOut(BaseModel):
    since: datetime | None
    total_calls: int
    success_count: int
    error_count: int
    cancelled_count: int
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
    """Latency / throughput / errors over the (filtered) inference logs. Both the
    counts and the percentiles are SQL aggregates on Postgres — no row transfer.
    The same filters as /logs apply, so the dashboard's filter bar scopes this too."""
    since = _naive_utc(since)
    conds = _log_conditions(status=status, provider=provider, model=model, q=q, since=since)

    query = select(
        func.count(),
        func.sum(case((InferenceLogRow.status == "success", 1), else_=0)),
        func.sum(case((IS_ERROR, 1), else_=0)),
        func.sum(case((IS_CANCELLED, 1), else_=0)),
        # Average only over calls whose latency means something.
        func.avg(case((HAS_USABLE_LATENCY, InferenceLogRow.latency_ms))),
        func.sum(InferenceLogRow.input_tokens),
        func.sum(InferenceLogRow.output_tokens),
    )
    for c in conds:
        query = query.where(c)

    total, success, errors, cancelled, avg_latency, itok, otok = session.exec(query).one()
    total = total or 0
    errors = errors or 0
    p50, p95, p99 = _percentiles(session, conds)
    return StatsOut(
        since=since,
        total_calls=total,
        success_count=success or 0,
        error_count=errors,
        cancelled_count=cancelled or 0,
        error_rate=(errors / total) if total else 0.0,
        avg_latency_ms=avg_latency,  # None when the window is empty
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
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
    """Per-bucket volume for the throughput/latency charts; only non-empty buckets
    are returned.

    On Postgres the bucketing is a `GROUP BY` on a floored epoch, so the database
    returns one row per bucket. SQLite has no `extract(epoch …)`, so tests fall
    back to bucketing the rows in Python — correct, but O(rows) in memory, which
    is why it is not the production path."""
    since = _naive_utc(since)
    bucket = max(1, bucket)
    conds = _log_conditions(status=status, provider=provider, model=model, q=q, since=since)
    errors_expr = func.sum(case((IS_ERROR, 1), else_=0))
    latency_expr = func.avg(case((HAS_USABLE_LATENCY, InferenceLogRow.latency_ms)))

    if _dialect(session) == "postgresql":
        # started_at is stored naive-UTC, so extracting the epoch needs no
        # timezone conversion; floor-divide into the bucket width and group.
        bucket_expr = (
            func.floor(func.extract("epoch", InferenceLogRow.started_at) / bucket)
            * bucket
        )
        query = select(
            bucket_expr,
            func.count(),
            errors_expr,
            latency_expr,
        ).group_by(bucket_expr).order_by(bucket_expr)
        for c in conds:
            query = query.where(c)
        points = [
            TimeseriesPoint(
                start=datetime.fromtimestamp(int(b), timezone.utc),
                calls=calls,
                errors=errs or 0,
                avg_latency_ms=avg_lat,
            )
            for b, calls, errs, avg_lat in session.exec(query).all()
        ]
        return TimeseriesOut(bucket_seconds=bucket, points=points)

    query = select(
        InferenceLogRow.started_at, InferenceLogRow.status, InferenceLogRow.latency_ms
    )
    for c in conds:
        query = query.where(c)

    buckets: dict[int, dict] = {}
    for started_at, status, lat in session.exec(query).all():
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        b = int(started_at.timestamp() // bucket) * bucket
        agg = buckets.setdefault(b, {"calls": 0, "errors": 0, "sum": 0.0, "n": 0})
        agg["calls"] += 1
        if status == "error":
            agg["errors"] += 1
        if lat is not None and status != "cancelled":
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
        func.sum(case((IS_ERROR, 1), else_=0)),
        func.avg(case((HAS_USABLE_LATENCY, InferenceLogRow.latency_ms))),
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
    # Bounded on purpose: an unbounded `limit` on an unauthenticated read is a
    # free way to make the service materialise the whole table. Page instead.
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
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


# Serve the built React dashboard at "/" (and its /assets), mounted LAST so all
# API routes above take precedence. Present in production / Docker; for local
# dashboard work use `cd dashboard && npm run dev` (Vite proxies to :8001).
if DASHBOARD_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DASHBOARD_DIST), html=True), name="dashboard-app")
else:

    @app.get("/", include_in_schema=False)
    def dashboard_root_not_built():
        return HTMLResponse(DASHBOARD_NOT_BUILT, status_code=503)
