"""Broker consumer: read inference logs from the Redis Stream and persist them.

Run as its own process:  python -m ingestion.worker

Uses a consumer group with acks (at-least-once): an entry is acked only after it
is stored, so a crash mid-processing redelivers it. Poison messages (bad JSON /
failed validation) are dropped after logging, so they don't redeliver forever.
"""

import logging
import os
import socket
import time

from redis import Redis
from redis import exceptions as redis_exc
from sqlmodel import Session

from db.engine import engine
from ingestion.store import store_log
from sdk.events import InferenceLog

STREAM = "inference_logs"
GROUP = "ingesters"

log = logging.getLogger(__name__)


class PoisonMessage(Exception):
    """An entry that can never be processed (bad JSON / fails validation)."""


def handle_entry(fields: dict, session: Session) -> None:
    """Parse one stream entry and store it. Raises PoisonMessage if the payload
    is unparseable/invalid; lets store errors propagate (so they redeliver)."""
    raw = fields.get("data")
    try:
        event = InferenceLog.model_validate_json(raw)
    except Exception as exc:  # bad JSON or schema violation — never processable
        raise PoisonMessage(str(exc)) from exc
    store_log(event, session)


def _ensure_group(client) -> None:
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):  # group already exists — fine
            raise


def run() -> None:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    _ensure_group(client)
    log.info("worker consuming %r in group %r as %r", STREAM, GROUP, consumer)
    while True:
        try:
            resp = client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10, block=5000)
        except redis_exc.TimeoutError:
            continue  # idle blocking read — just wait again
        except redis_exc.RedisError as exc:
            log.warning("redis read failed, retrying: %r", exc)
            time.sleep(1)
            continue
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                with Session(engine) as session:
                    try:
                        handle_entry(fields, session)
                    except PoisonMessage as exc:
                        log.warning("dropping poison entry %s: %s", entry_id, exc)
                        client.xack(STREAM, GROUP, entry_id)  # drop, don't redeliver
                    except Exception as exc:  # transient (e.g. DB down) — redeliver
                        log.warning("store failed for %s, will retry: %r", entry_id, exc)
                    else:
                        client.xack(STREAM, GROUP, entry_id)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run()
