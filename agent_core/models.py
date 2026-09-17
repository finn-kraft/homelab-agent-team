from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    VERIFYING = "verifying"
    CHECKPOINTING = "checkpointing"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    VERIFICATION = "verification"
    CHECKPOINT = "checkpoint"
    CHANGES_REQUESTED = "changes_requested"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Job:
    id: int
    goal: str
    repository: str
    branch: str
    status: JobStatus = JobStatus.PENDING
    priority: int = 0
    current_phase: str | None = None
    current_step: int | None = None
    iteration_count: int = 0
    max_iterations: int = 100
    human_notes: str | None = None


@dataclass(slots=True)
class Step:
    id: int
    job_id: int
    sequence: int
    title: str
    objective: str
    rationale: str
    acceptance_criteria: list[str]
    constraints: list[str] = field(default_factory=list)
    suggested_files: list[str] = field(default_factory=list)
    dependencies: list[int] = field(default_factory=list)
    assigned_agent: str = "engineering-agent"
    status: StepStatus = StepStatus.QUEUED
    attempt_count: int = 0
    reviewer_feedback: dict[str, Any] | None = None
    coder_response: dict[str, Any] | None = None
    resulting_commit: str | None = None


@dataclass(slots=True)
class TaskContract:
    job_id: int
    step_id: int
    title: str
    objective: str
    acceptance_criteria: list[str]
    constraints: list[str]
    suggested_files: list[str]
    assigned_agent: str = "engineering-agent"


@dataclass(slots=True)
class ReviewFeedback:
    verdict: str
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReviewRequest:
    job_id: int
    step_id: int
    review_attempt: int
    task: TaskContract
    repository: dict[str, Any]
    implementation: ImplementationResult


@dataclass(slots=True)
class ReviewIssue:
    stable_issue_key: str
    severity: str
    category: str
    problem: str
    evidence: str
    requested_change: str
    file: str | None = None
    line: int | None = None
    status: str = "open"


@dataclass(slots=True)
class ImplementationResult:
    step_id: int
    summary: str
    files_changed: list[str]
    verification: list[dict[str, Any]]
    commit_sha: str | None = None


@dataclass(slots=True)
class VerificationResult:
    """Result of the deterministic post-review verification gate."""

    step_id: int
    passed: bool
    commands: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class CheckpointResult:
    """Result of the controlled Git checkpoint operation."""

    step_id: int
    created: bool
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    message: str = ""
    error: str | None = None


@dataclass(slots=True)
class WorkerLease:
    """Durable ownership record used to prevent competing workers."""

    resource: str
    worker_id: str
    expires_at: datetime
    job_id: int | None = None
    step_id: int | None = None


@dataclass(slots=True)
class ModelRoute:
    """Auditable inference-routing decision, independent of workflow state."""

    caller_agent: str
    provider: str
    model: str
    location: str
    reason: str
    attempt: int
    task_type: str | None = None
    fallback: bool = False
    privacy_sensitive: bool = False
    latency_seconds: float | None = None
    usage: dict[str, Any] | None = None
    estimated_cloud_cost: float | None = None
    timestamp: datetime | None = None


@dataclass(slots=True)
class AgentEvent:
    job_id: int
    event_type: str
    structured_payload: dict[str, Any]
    agent: str
    step_id: int | None = None
    timestamp: datetime | None = None


@dataclass(slots=True)
class PlannerDecision:
    decision: str
    job_status: str
    reasoning_summary: str
    step: dict[str, Any] | None = None
    evidence: list[str] = field(default_factory=list)
    human_question: str | None = None
    blocker: str | None = None
    model: str | None = None
    provider: str | None = None
    latency_seconds: float | None = None
    usage: dict[str, Any] | None = None
    # Optional bounded batch emitted by V2-aware planners. ``step`` remains
    # the first item for V1 callers and older model responses.
    steps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
