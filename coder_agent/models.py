from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    CHANGES_REQUESTED = "changes_requested"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    PAUSED = "paused"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Task:
    job_id: int
    step_id: int
    repository: str
    branch: str
    objective: str
    acceptance_criteria: list[str]
    constraints: list[str] = field(default_factory=list)
    status: Status = Status.QUEUED
    attempt: int = 0
    reviewer_feedback: dict[str, Any] | None = None


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False


@dataclass(slots=True)
class AgentResult:
    status: Status
    summary: str
    files_changed: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    blocker: str | None = None
    model: str | None = None
    completed_at: datetime | None = None

