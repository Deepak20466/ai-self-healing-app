"""Structured JSON logging shared by every pod.

Uses stdlib `logging` as the transport and `structlog` to render every record
(including ones from third-party libraries) as a single JSON object per line,
which is what the sentinel capture pipeline and cloud log collectors expect.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib logging + structlog for JSON output. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    root_logger.handlers = []
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to `name`. Configures logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
