from __future__ import annotations

"""Shared redaction for content crossing durable audit boundaries."""

import re
from typing import Any


_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,}]+"),
    re.compile(r"\bpostgres(?:ql)?(?:\+[A-Za-z0-9_-]+)?://[^\s]+"),
    re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)


def redact_text(value: Any, *, limit: int | None = None) -> str:
    redacted = str(value)
    for pattern in _PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:limit] if limit is not None else redacted


def redact_payload(value: Any) -> Any:
    """Return a recursively redacted copy suitable for durable JSON."""
    if isinstance(value, dict):
        return {str(key): redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
