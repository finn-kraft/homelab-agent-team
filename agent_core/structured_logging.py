from __future__ import annotations

"""One-line structured logs with a deliberately narrow data boundary.

Application logs are operational metadata, not a second copy of prompts,
repository content, diffs, or command output.  Callers may attach explicit
fields with :func:`log_event`; high-risk content fields are omitted and all
remaining strings pass through the shared secret redactor.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from .redaction import redact_payload, redact_text


_OMITTED_FIELDS = {
    "body",
    "command_output",
    "content",
    "diff",
    "document",
    "file_content",
    "prompt",
    "repository_content",
    "response_body",
    "source",
    "stderr",
    "stdout",
}


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "[OMITTED]"
                    if str(key).lower() in _OMITTED_FIELDS
                    else scrub(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    safe = scrub(fields)
    redacted = redact_payload(safe)
    return redacted if isinstance(redacted, dict) else {}


class StructuredJsonFormatter(logging.Formatter):
    """Render stable JSON without serializing arbitrary ``LogRecord`` data."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        event = getattr(record, "event_name", None) or "log"
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "event": redact_text(event, limit=128),
        }
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, dict):
            payload.update(_safe_fields(fields))
        else:
            payload["message"] = redact_text(record.getMessage(), limit=8_000)

        if record.exc_info and record.exc_info[1] is not None:
            exception = record.exc_info[1]
            payload["exception"] = {
                "type": type(exception).__name__,
                # Exception strings may embed a model response, a command
                # fragment, or repository content. The structured event and
                # exception class retain operational value without copying it.
                "message": "[OMITTED]",
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(
    service: str,
    *,
    level: int | str | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure the process root logger for one JSON record per journal line."""
    selected = level or os.getenv("AGENT_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(StructuredJsonFormatter(service))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(selected)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit an event with only explicitly supplied, policy-filtered fields."""
    logger.log(
        level,
        event,
        extra={"event_name": event, "event_fields": fields},
    )
