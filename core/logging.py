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


def _scrub_event_dict(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Redact secrets from every log record globally, across all 4 pods.

    Defense-in-depth on top of scrubbing at the point a secret would be
    embedded (e.g. never putting a credentialed URL in an exception
    message) -- a real credentialed-URL leak reached `healer.log` via an
    *unhandled* exception's traceback (a git push failure whose message
    embedded a token-bearing remote URL), which bypasses any scrubbing done
    at a specific call site. Runs as the last processor before rendering, so
    it sees the fully-formatted `event`/`exception` text (including
    `format_exc_info`'s rendered traceback), not just structured kwargs.
    Import is local to avoid `sentinel` importing `core` importing `sentinel`
    at module-load time in pods that don't otherwise need sentinel code.
    """
    from sentinel.scrubber import scrub_value

    return {key: scrub_value(value) for key, value in event_dict.items()}


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
        _scrub_event_dict,
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
