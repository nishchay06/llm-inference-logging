from .events import InferenceLog


def emit(event: InferenceLog) -> None:
    """Where a captured inference log goes.

    For now it just prints. This is the seam that changes as we climb:
      - Rung 4: POST the event to the ingestion service.
      - Rung 5: make it non-blocking and failure-safe.
    The chat code never knows or cares which of these is wired up.
    """
    print("---- inference log ----")
    print(event.model_dump_json(indent=2))
