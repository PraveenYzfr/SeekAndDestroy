"""The queue between "somebody pressed the button" and the worker that indexes.

A Redis list used as a FIFO: the API LPUSHes a run id, the worker BRPOPs it.

WHY REDIS CARRIES THE REQUEST AND SQL SERVER CARRIES THE RUN
------------------------------------------------------------
These are different kinds of data and they get different storage.

The queue entry is a *request* - "run id 41 wants indexing". It is small, it is
short-lived, and losing it is survivable: the run row still exists as Queued, and
a human can requeue it. Redis here runs with a 256 MB cap and an eviction policy,
which is exactly right for that and exactly wrong for history.

The run row is *history* - what ran, when, how far it got, why it stopped. That
belongs in the database, and it is written **before** the queue entry, so a
crash between the two leaves a visible Queued run rather than a silent nothing.

WHY NOT CELERY OR RQ
--------------------
Both would bring a broker abstraction, a result backend and a worker supervisor
for one job type with one consumer. The queue semantics needed here are "one
list, blocking pop, at-most-once", which is a Redis primitive. The parts that
actually need care - the single-run lease, per-batch checkpointing, heartbeats
and reclaiming abandoned runs - are in SQL Server where they can be reasoned
about transactionally, and no task framework would provide them.

AT-MOST-ONCE, DELIBERATELY
--------------------------
BRPOP removes the entry before the worker starts. If the worker dies mid-run the
entry is gone, the run row is reclaimed as Abandoned by its stale heartbeat, and
nobody retries automatically. That is the right default for a job that spends
money at an embedding provider: an automatic retry loop against a failing
provider is how a rate limit becomes a bill. Requeueing is a deliberate act.
"""

from __future__ import annotations

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

#: Single queue, single consumer. Named rather than reusing a cache key prefix so
#: that flushing the cache can never drop queued work.
QUEUE_KEY = "sad:index:queue"

#: How long BRPOP blocks before returning empty. Not a poll interval - the pop is
#: genuinely blocking - but a timeout the worker needs so it can notice shutdown
#: signals and re-check for abandoned runs rather than blocking forever.
BLOCK_SECONDS = 5


class QueueUnavailable(RuntimeError):
    """Redis could not be reached. Raised rather than swallowed: a trigger that
    silently fails to enqueue would leave a Queued run nothing will ever pick
    up, which looks identical to a slow worker."""


def _client():
    import redis

    settings = get_settings().cache
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue(run_id: int) -> None:
    """Publish a run id. The run row must already exist."""
    try:
        _client().lpush(QUEUE_KEY, str(run_id))
    except Exception as exc:  # redis-py raises a family of connection errors
        raise QueueUnavailable(str(exc)) from exc


def dequeue(block_seconds: int = BLOCK_SECONDS) -> int | None:
    """Block for the next run id, or return None if the wait elapsed."""
    try:
        item = _client().brpop(QUEUE_KEY, timeout=block_seconds)
    except Exception as exc:
        raise QueueUnavailable(str(exc)) from exc
    if not item:
        return None
    _, value = item
    try:
        return int(value)
    except (TypeError, ValueError):
        # Something else wrote to this key. Drop it rather than crashing the
        # worker loop, but say so - a queue with foreign traffic in it is a
        # misconfiguration worth seeing.
        logger.warning("index_queue.discarded_unparseable_entry", value=str(value)[:100])
        return None


def depth() -> int:
    """Entries waiting. Reported by the status endpoint so a run that is Queued
    for a long time can be distinguished from a worker that is not running."""
    try:
        return int(_client().llen(QUEUE_KEY))
    except Exception as exc:
        raise QueueUnavailable(str(exc)) from exc
