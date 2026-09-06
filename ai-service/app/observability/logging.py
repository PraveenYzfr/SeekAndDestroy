"""One logging configuration, for every process in this image.

WHY THIS IS A MODULE AND NOT SIX LINES IN main.py
--------------------------------------------------
It WAS six lines in main.py, and ai-indexer runs ``python -m
app.retrieval.worker`` - a different command against the same image, which never
imports ``app.main``. So ``structlog.configure()`` never ran there at all: the
worker's log events came out under structlog's defaults, neither level-filtered
nor rendered by anything this platform chose.

That is worse than one bad format applied consistently. A shipper pointed at
this estate would receive TWO different shapes from one platform and have to
guess which parser to use per container, and the container emitting the odd
shape is the one doing the long, unattended, easy-to-ignore work.

Setting SAD_SERVICE__LOG_JSON in the compose environment does not fix it either.
The worker reads the same settings and then renders with a configuration it
never applied - the value is correct and has no effect, which is the hardest
kind of wrong to see.

CALLED BY EVERY ENTRYPOINT. If a third command is ever added to this image, it
calls this too.
"""
from __future__ import annotations

import logging

import structlog

from app.config import get_settings

_configured = False


def configure_logging() -> None:
    """Apply the platform's log configuration to this process.

    Idempotent: calling it twice is a no-op rather than a second, subtly
    different, configuration. An entrypoint that imports another entrypoint
    should not end up with whichever ran last.

    PrintLogger is left as the factory, deliberately. It writes to STDOUT, which
    is what a container log collector reads - so stdout-tail shipping needs no
    stdlib bridge and works today. The bridge only matters for handler-based
    shipping in-process, which this platform does not do.
    """
    global _configured
    if _configured:
        return

    settings = get_settings().service
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # JSON in a container, ConsoleRenderer at a terminal. The default is
            # the terminal one, which is right for a developer and wrong in
            # prod: it writes ANSI escape codes into the docker log, where
            # nothing renders them and every structured query has to strip them
            # before it can filter on a field.
            structlog.processors.JSONRenderer()
            if settings.log_json
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    _configured = True
