# Rung 6 Design — Persist to Postgres

The schema rung: move data out of process RAM and the print statements into a
real database. This is what the assignment grades on — "sensible schema design
and practical tradeoffs."

> **Two deviations from this original plan, discovered during implementation**
> (kept here to show how the design evolved):
> 1. **Docker Compose arrived later, as its own bonus.** For a while we ran a
>    **standalone local Postgres**; the one-command `docker compose up`
>    (Postgres + both services + a one-shot schema init) now exists at the repo
>    root. Local manual setup is still fully supported.
> 2. **Schema is created out-of-band, not on startup.** `init_db()` is **not**
>    called on app boot (that caused a concurrent-DDL race between the two
>    services); it's a one-time `python -m db.init`. See the Migrations section.

## Decisions

- **Database:** Postgres from the start, run via **Docker Compose** (this also
  seeds the Docker one-command-setup bonus). One shared Postgres instance.
- **ORM:** **SQLModel** (Pydantic + SQLAlchemy) — same mental model as our
  existing Pydantic `InferenceLog`, and it plays natively with FastAPI.
- **Ownership (Option A):** the **chatbot** owns `conversations` + `messages`;
  the **ingestion** service owns `inference_logs`. Both connect to the same
  Postgres but write different tables. Each service owns its own data.

## Layering — keep the SDK free of the database

The client SDK (`sdk/`) must NOT depend on SQLModel or the DB. So:

- `sdk/events.py::InferenceLog` stays a **pure Pydantic model** — the wire
  contract between chatbot and ingestion.
- `db/models.py::InferenceLogRow` is a separate **SQLModel table** — the storage
  shape. Ingestion receives an `InferenceLog` (validated) and maps it to an
  `InferenceLogRow` to insert. Fields overlap; the mapping is trivial
  (`InferenceLogRow(**event.model_dump())`).

This "wire model ≠ storage model" split is deliberate and worth noting in the
README: the transport contract and the database schema can evolve independently.

## Schema — three tables

```
conversations                 messages                       inference_logs
-------------                 --------                       --------------
session_id  PK (uuid str)     id           PK (auto)         event_id     PK (uuid str)
created_at                    session_id   FK -> conv        session_id   FK -> conv (indexed)
updated_at                    role         (user|assistant)  provider
                              content      (full text)       model
                              created_at   (indexed)         status       (indexed)
                                                             error_type / error_message
                                                             started_at (indexed) / ended_at
                                                             latency_ms
                                                             input_tokens / output_tokens
                                                             input_preview / output_preview
                                                             created_at
```

Relationships: `conversations` 1—* `messages`, and `conversations` 1—*
`inference_logs`, both keyed by `session_id`.

**Indexes** (chosen for the queries we'll actually run):
- `messages(session_id)` — rebuild a conversation / the context window.
- `inference_logs(session_id)` — all calls for a conversation.
- `inference_logs(started_at)` — time-series dashboard queries (Rung 7+).
- `inference_logs(status)` — error-rate queries.

## The graded tradeoff — why messages and inference_logs are separate tables

They are different *kinds* of data:
- `messages` = transactional application data (a user's actual conversation).
- `inference_logs` = high-volume, append-only **telemetry** — one row per LLM
  call, including retries and errors that never became a visible message.

Keeping them separate lets each scale and evolve independently (you might prune
or archive telemetry aggressively while keeping conversations), and keeps the
messages table clean of observability columns. This reasoning goes in the
README's "schema design decisions."

## Who writes what / connection

- Both services read `DATABASE_URL` from the environment.
- **chatbot** `/chat`: upsert the `conversation`, insert the user `message` and
  the assistant `message`. This **replaces the in-memory `CONVERSATIONS` dict** —
  and the context window is now built by querying the last N `messages` for the
  session. (Fixes the "lost on restart" tradeoff from Rung 2.)
- **ingestion** `POST /logs`: insert an `InferenceLogRow`. This **replaces the
  print**.

## Migrations

Schema is created by a one-time `python -m db.init` step
(`SQLModel.metadata.create_all`), **not** on app startup. Reason (found during
implementation): two services each calling `create_all` on boot race on DDL and
one crashes with a Postgres catalog unique-violation. Single-owner schema
creation avoids that. A real system uses **Alembic** for versioned migrations —
a "what I'd improve with more time" item.

## Testing

Unit tests run against **in-memory SQLite** (fast, no Postgres needed) via a
SQLModel engine fixture — SQLModel makes the same models work on both. We'll note
the SQLite-vs-Postgres caveat. The app itself runs on Postgres.

## Package layout

```
db/
  __init__.py
  models.py     Conversation, Message, InferenceLogRow (SQLModel tables)
  engine.py     engine from DATABASE_URL, get_session(), init_db()
docker-compose.yml   postgres service (chatbot + ingestion added later)
```

## Build steps

1. `docker-compose.yml` with a `postgres` service; bring it up; add `DATABASE_URL`.
2. Add deps: `sqlmodel`, `psycopg[binary]` (Postgres driver).
3. `db/models.py` + `db/engine.py`.
4. **ingestion**: `init_db()` on startup; `POST /logs` inserts an `InferenceLogRow`.
5. **chatbot**: `init_db()` on startup; `/chat` persists conversation + messages;
   context window reads from the DB. Retire the in-memory dict.
6. Verify: bring up Postgres + both services, `POST /chat`, then query the tables
   to see conversation/message/log rows.
7. Tests: DB-write tests on in-memory SQLite; keep the existing 7 green.

## The "why" to master

*Should chat messages and inference logs live in the same table?* — No, and be
able to say why (transactional app data vs append-only telemetry; different
volume, lifecycle, and scaling). Verify by looking at the three clean tables and
the queries each index serves.
