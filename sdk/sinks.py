import httpx

from .events import InferenceLog


def print_sink(event: InferenceLog) -> None:
    """The Rung 3 sink: just print. Kept as a simple reference / fallback."""
    print("---- inference log ----")
    print(event.model_dump_json(indent=2))


class HttpSink:
    """Rung 4 sink: POST the log to the ingestion service.

    Deliberately CRUDE: the POST is synchronous and unguarded, so the chat
    request blocks on it and will FAIL if ingestion is slow or down. That
    coupling is exactly the problem Rung 5 fixes (non-blocking, failure-safe).

    Note we send `event.model_dump_json()` as the request body (not `json=`):
    Pydantic serializes the datetime fields to ISO strings, which the stdlib
    JSON encoder behind `json=` cannot do.
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
