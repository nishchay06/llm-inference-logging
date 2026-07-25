# Event-Based Architecture Design — durable broker + worker

Replace the in-process `QueueSink` hand-off with a **durable message broker**, so
the log path survives an ingestion outage or a chatbot crash. Producer and
consumer are decoupled through the broker.

## The gap this closes
Today: chatbot → `TracedClient` → `QueueSink` → `HttpSink` → `POST /logs`. If
ingestion is **down**, the POST fails and `QueueSink` **swallows and drops** the
event. The in-process queue survives *slowness*, not an outage or a crash.

With a broker: the producer hands the event to the broker; if the consumer is
down, events **wait durably in the broker** and are processed on its return
(**at-least-once**, via a consumer group + acks). That is the upgrade —
consumer-side durability + decoupling + replay.

## Inspiration (log/telemetry pipelines)
Every real pipeline is **producer → durable broker → consumer worker → store**
(Kafka, Redis Streams, Kinesis): an append-only stream, **consumer groups** so
workers share load, **at-least-once + acks** (unacked messages redeliver after a
crash), and a **dead-letter** path for poison messages. Our `QueueSink` is the
single-process seed of exactly this (see [sdk-design.md](./sdk-design.md)).

## Decisions
- **Broker: Redis Streams** — one lightweight container; `XADD` append, consumer
  groups, `XACK`, replay. Far lighter than Kafka/RabbitMQ, perfect fit for a log
  stream.
- **Consumer: a separate worker process** (`python -m ingestion.worker`) — the
  textbook producer/broker/consumer shape, independently scalable.
- **Only the SDK's *sink* changes** — `TracedClient` is untouched (the 3-concern
  split paying off).
- **Opt-in via `REDIS_URL`** — set → broker path; unset → today's HTTP path. Keeps
  local dev and the test suite simple; no Redis needed to run or test.

## Components
- **Producer — `sdk/sinks.py::RedisStreamSink`**: `XADD <stream> {data: <json>}`.
  Still wrapped in `QueueSink` so the chat never blocks/breaks (Redis down → the
  background worker's `XADD` fails and is swallowed, as before).
- **Shared store — `ingestion` `store_log(event, session)`**: map `InferenceLog`
  → `InferenceLogRow`, insert. Extracted from today's `POST /logs` handler and
  reused by the worker (one code path for validation + persistence).
- **Consumer — `ingestion/worker.py`**: ensure the consumer group exists
  (`XGROUP CREATE … MKSTREAM`, ignore BUSYGROUP); loop `XREADGROUP group consumer
  {stream: ">"} block=…`; for each entry parse JSON → `InferenceLog` → `store_log`
  → `XACK`. Validation/parse failure → log + `XACK` (drop the poison message, no
  redelivery loop); store failure → **don't ack** (redeliver on next pass).
- **Ingestion HTTP service** keeps `/stats*`, `/logs` (query), `/dashboard`, and
  `POST /logs` (still works via `store_log` — a compat/fallback path).

Constants: stream `inference_logs`, group `ingesters`.

## Config / infra
- Dep: `redis` (redis-py). `.env.example`: `REDIS_URL` (e.g.
  `redis://localhost:6379/0`).
- Producer sink factory (in `app/main.py`): `REDIS_URL` set →
  `QueueSink(RedisStreamSink(...))`; else `QueueSink(HttpSink(INGESTION_URL))`.
- Docker Compose: add **`redis`** (redis:7-alpine) and a **`worker`** service
  (`python -m ingestion.worker`, depends on `db-init` + `redis`, env `REDIS_URL`
  + `DATABASE_URL`). Chatbot gains `REDIS_URL` (→ broker path). Worker + chatbot
  need Redis; the ingestion HTTP API does not.

## Failure model (updated ARCHITECTURE.md)
- Chat never blocks (unchanged).
- **Ingestion/worker outage no longer loses logs** — they persist in the stream;
  the worker replays unacked entries on restart (at-least-once → consumers must
  tolerate duplicates; `event_id` PK makes the insert idempotent-ish — a dup
  insert fails the row, not the batch).
- Poison messages are dropped (logged), not infinitely redelivered.
- Remaining small window: events sitting in the in-process `QueueSink` when the
  chatbot crashes before `XADD`. Documented tradeoff (a synchronous durable write
  would block the chat — not worth it).

## TDD (fakes; no Redis server)
- `RedisStreamSink` — a fake redis records `xadd(stream, {"data": json})`; assert
  the event is serialized and added to the right stream.
- `store_log` — stores an `InferenceLogRow` from an `InferenceLog` (SQLite).
- worker entry-handler — a good entry → `store_log` called + ack; a poison entry
  (bad JSON) → dropped, no raise.
- Live-verify the real `XADD → worker → Postgres` chain in Docker Compose.

## Deferred
Kafka/partitions, a real dead-letter stream (we just drop+log), exactly-once,
batched `XADD`, consumer autoscaling.
