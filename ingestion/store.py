"""Shared persistence for an inference log — used by both the HTTP endpoint
(POST /logs) and the broker worker, so validation + storage is one code path."""

from sqlmodel import Session

from db.models import InferenceLogRow
from sdk.events import InferenceLog


def store_log(event: InferenceLog, session: Session) -> None:
    """Map the validated wire model to the storage row and insert it.

    Timestamps arrive timezone-aware (the SDK stamps UTC and JSON carries the
    offset); normalising them to naive UTC is the `UtcDateTime` column's job, so
    it holds for every write path rather than just this one.
    """
    session.add(InferenceLogRow(**event.model_dump()))
    session.commit()
