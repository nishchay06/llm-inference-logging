import queue
import threading
from typing import Callable

import httpx

from .events import InferenceLog


def print_sink(event: InferenceLog) -> None:
    """The Rung 3 sink: just print. Kept as a simple reference / fallback."""
    print("---- inference log ----")
    print(event.model_dump_json(indent=2))


class RedisStreamSink:
    """Publish the log to a Redis Stream (the event-based transport). `XADD`
    appends the event as JSON; a separate worker consumes and persists it.

    Like HttpSink, this is synchronous/unguarded on its own — wrap it in a
    QueueSink so a slow/unavailable broker never blocks or breaks the chat.
    Durability past the hand-off is the broker's job (the consumer can be down
    and events wait in the stream)."""

    def __init__(self, client, stream: str = "inference_logs"):
        self._client = client
        self._stream = stream

    def __call__(self, event: InferenceLog) -> None:
        self._client.xadd(self._stream, {"data": event.model_dump_json()})


class HttpSink:
    """POST the log to the ingestion service.

    On its own this is synchronous and unguarded — a failed POST raises. Wrap it
    in a QueueSink (below) to make it non-blocking and failure-safe.

    We send `event.model_dump_json()` as the body (not `json=`): Pydantic
    serializes the datetime fields to ISO strings, which the stdlib JSON encoder
    behind `json=` cannot do.
    """

    def __init__(self, url: str, timeout: float = 5.0):
        self._url = url
        self._timeout = timeout

    def __call__(self, event: InferenceLog) -> None:
        resp = httpx.post(
            self._url,
            content=event.model_dump_json(),
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()


class QueueSink:
    """Makes any inner sink non-blocking and failure-safe (Rung 5).

    Enqueuing an event returns immediately — the chat never waits for delivery.
    A background daemon thread drains the queue and calls the inner sink; if
    delivery fails, the error is logged and the event dropped, never surfacing
    to the caller. This is the classic producer/consumer pattern, and the seed
    for Rung 8's external (Redis/Kafka) queue — there, only the queue changes.
    """

    def __init__(
        self,
        inner: Callable[[InferenceLog], None],
        max_queue: int = 1000,
    ):
        self._inner = inner
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def __call__(self, event: InferenceLog) -> None:
        # Non-blocking: if the queue is full we DROP rather than block the chat.
        # Losing telemetry is acceptable; blocking the user's request is not.
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            print("WARNING: inference-log queue is full; dropping an event")

    def _worker(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._inner(event)
            except Exception as exc:  # delivery failure must never escape
                print(f"WARNING: failed to ship inference log: {exc!r}")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue is drained (for tests / graceful shutdown)."""
        done = threading.Event()

        def _wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)
