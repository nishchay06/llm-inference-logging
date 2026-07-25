"""Tests for QueueSink — the non-blocking, failure-safe sink.

The safety guarantee: enqueuing an event returns immediately and never raises,
even when the underlying delivery fails. Delivery happens on a background
thread; failures there are swallowed, never surfaced to the caller.
"""

from sdk.sinks import QueueSink


def test_delivers_events_in_order():
    received = []
    sink = QueueSink(received.append)

    sink("evt1")
    sink("evt2")
    sink.flush()  # wait for the background worker to drain

    assert received == ["evt1", "evt2"]


def test_enqueue_never_raises_when_delivery_fails():
    attempts = []

    def exploding(event):
        attempts.append(event)
        raise RuntimeError("ingestion down")

    sink = QueueSink(exploding)

    # Enqueuing must return immediately and must NOT raise, even though the
    # background delivery is guaranteed to fail.
    sink("evt")  # <-- no exception here

    sink.flush()
    assert attempts == ["evt"]  # delivery WAS attempted (and the error swallowed)
