"""Canonical workflow vocabulary shared by V1-compatible transports."""

from __future__ import annotations


PHASE_ALIASES = {
    "coding": "engineering",
    "coder": "engineering",
    "coder_revision": "engineering",
    "engineering_revision": "engineering",
}
AGENT_ALIASES = {"coder-agent": "engineering-agent"}


def canonical_phase(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return PHASE_ALIASES.get(normalized, normalized)


def canonical_agent(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return AGENT_ALIASES.get(normalized, normalized)


def canonical_next_state(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return "engineering_revision" if normalized == "coder_revision" else normalized
