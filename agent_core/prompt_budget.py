"""Small, dependency-free helpers for bounded model prompts.

Model providers count the entire conversation, not just the latest user
message. These helpers preserve the system prompt and newest context while
dropping the oldest conversational turns once the configured character budget
is reached.
"""

from __future__ import annotations

import json
from typing import Any


DEFAULT_PROMPT_CHARS = 120_000


def bounded_text(value: Any, limit: int, marker: str = "[TRUNCATED]") -> str:
    """Return text no longer than ``limit`` characters, including the marker."""
    limit = max(1, int(limit))
    text = str(value or "")
    if len(text) <= limit:
        return text
    suffix = f"\n{marker}"
    if len(suffix) >= limit:
        return suffix[:limit]
    return text[: limit - len(suffix)] + suffix


def bounded_json(value: Any, limit: int, label: str = "context") -> str:
    """Serialize JSON while guaranteeing a valid, bounded result."""
    limit = max(1, int(limit))
    encoded = json.dumps(value, default=str)
    if len(encoded) <= limit:
        return encoded
    if limit < 2:
        return "0"
    if limit < 256:
        return "{}"
    # Keep the compact representation valid JSON. The original structured
    # payload is retained as a string so no malformed request reaches a model.
    notice = {
        "context_notice": f"{label} exceeded the configured prompt budget and was truncated",
        "payload": encoded,
    }
    for size in (limit, max(256, limit // 2), 256):
        notice["payload"] = bounded_text(encoded, size)
        result = json.dumps(notice, default=str)
        if len(result) <= limit:
            return result
    # The marker and notice are always tiny, but keep a final defensive path
    # for a caller that supplies an unusually small budget.
    return json.dumps({"context_notice": f"{label} omitted"})


def bounded_messages(messages: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    """Keep a message list within a strict character budget.

    The first system message and newest turns are the most useful. Older
    assistant/user observations are discarded first; the newest message is
    truncated only if it cannot fit by itself.
    """
    limit = max(1, int(limit))
    if not messages:
        return []

    system = dict(messages[0])
    system["content"] = str(system.get("content", ""))
    if len(system["content"]) >= limit:
        system["content"] = bounded_text(system["content"], limit)
        return [system]

    selected: list[dict[str, str]] = []
    remaining = limit - len(system["content"])
    for message in reversed(messages[1:]):
        value = dict(message)
        content = str(value.get("content", ""))
        if remaining <= 0:
            break
        if len(content) > remaining:
            value["content"] = bounded_text(content, remaining)
            selected.append(value)
            remaining = 0
            break
        selected.append(value)
        remaining -= len(content)
    selected.reverse()
    return [system, *selected]


def message_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages)
