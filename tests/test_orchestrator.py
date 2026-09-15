"""State-machine tests for the deterministic workflow coordinator.

These intentionally use small in-memory doubles rather than PostgreSQL or an
LLM.  The production store is the durable implementation; these tests prove
that the coordinator's transition decisions are based on durable statuses and
verification/checkpoint results rather than model output.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from coder_agent.models import Status, Task
from orchestrator.config import OrchestratorConfig
from orchestrator.orchestrator import AgentOrchestrator


@dataclass
class _Work:
    id: int = 7
    job_id: int = 1
    repository: str = "/safe/worktree"
    branch: str = "agents/autonomous-align"
    title: str = "Add a safe health endpoint"
    starting_commit: str = "a" * 40
    files_changed: list[str] | None = None
    approved_files: list[str] | None = None
    checkpoint_marker: str = "autonomous-step:1:7:test"
    attempt_count: int = 0


class StateStore:
    """A minimal durable-state double with the OrchestratorStore API."""

    def __init__(self, state: str | None = None, job_status: str = "running") -> None:
        self.state = state
        self.job_status = job_status
        self.work = _Work(files_changed=[], approved_files=[])
        self.events: list[tuple[str, object]] = []
        self.completed_steps: list[int] = []
        self.released_locks: list[tuple[str, str]] = []

    @property
    def active(self) -> bool:
        return self.job_status not in {"paused", "cancelled", "needs_human", "blocked"}

    def recover_expired(self):
        return []

    def claim_verification(self, worker_id, lease_seconds, repository_lock_seconds):
        if self.active and self.state == "verification":
            return self._work_dict()
        return None

    def claim_checkpoint(self, worker_id, lease_seconds, repository_lock_seconds):
        if self.active and self.state == "checkpoint":
            return self._work_dict()
        return None

    def next_review_step(self):
        if self.active and self.state == "review":
            self.job_status = "reviewing"
            return self.work.id
        return None

    def claim_coding(self, worker_id, lease_seconds, repository_lock_seconds):
        if not self.active or self.state not in {"queued", "changes_requested"}:
            return None
        self.state = "running"
        self.work.attempt_count += 1
        return Task(
            self.work.job_id,
            self.work.id,
            self.work.repository,
            self.work.branch,
            "Implement the approved task",
            ["Feature works", "Tests pass"],
            [],
            Status.RUNNING,
            self.work.attempt_count,
            None,
        )

    def finish_coding_handoff(self, step_id, worker_id):
        assert step_id == self.work.id
        if self.state == "review":
            self.job_status = "reviewing"
        return self.state

    def finish_review_handoff(self, step_id):
        assert step_id == self.work.id
        if self.state == "changes_requested":
            self.job_status = "running"
        elif self.state == "verification":
            self.job_status = "verifying"
        return self.state

    def record_verification(self, work, worker_id, result):
        assert work["id"] == self.work.id
        if result.passed:
            self.state = "checkpoint"
            self.job_status = "checkpointing"
            self.work.approved_files = list(self.work.files_changed or [])
            return "checkpoint"
        self.state = "changes_requested"
        self.job_status = "running"
        return "changes_requested"

    def record_checkpoint(self, work, worker_id, result):
        assert work["id"] == self.work.id
        if result.success:
            self.state = "complete"
            self.job_status = "running"
            self.completed_steps.append(self.work.id)
            return "complete"
        self.state = "changes_requested" if result.retryable else "needs_human"
        self.job_status = "running" if result.retryable else "needs_human"
        return self.state

    def release_repository_lock(self, repository, owner):
        self.released_locks.append((repository, owner))

    def step(self, step_id):
        assert step_id == self.work.id
        return self._work_dict()

    def preexisting_files(self, step_id):
        assert step_id == self.work.id
        return []

    def record_model_invocation(self, **payload):
        self.events.append(("model_route", payload))

    def _work_dict(self):
        return {
            "id": self.work.id,
            "job_id": self.work.job_id,
            "repository": self.work.repository,
            "branch": self.work.branch,
            "title": self.work.title,
            "starting_commit": self.work.starting_commit,
            "files_changed": list(self.work.files_changed or []),
            "approved_files": list(self.work.approved_files or []),
            "checkpoint_marker": self.work.checkpoint_marker,
            "attempt_count": self.work.attempt_count,
        }


class Planner:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.last_job_id = 1
        self.calls = 0

    def plan_once(self):
        if not self.store.active:
            return None
        self.calls += 1
        # A planner invocation represents the durable decision to queue the
        # next bounded task.  It is intentionally invoked again after a step
        # checkpoint, not only once at job creation.
        self.store.state = "queued"
        self.store.job_status = "running"
        return SimpleNamespace(decision="create_step")


class Coder:
    worker_id = "coder-test"

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.tasks: list[Task] = []
        self.router = SimpleNamespace(last_route=None)

    def run_task(self, task):
        self.tasks.append(task)
        self.store.work.files_changed = ["feature.py"]
        self.store.state = "review"
        return SimpleNamespace(summary=f"implemented attempt {task.attempt}")


class Reviewer:
    def __init__(self, store: StateStore, verdicts: list[str]) -> None:
        self.store = store
        self.verdicts = list(verdicts)
        self.step_ids: list[int] = []
        self.router = SimpleNamespace(last_route=None)

    def review_once(self, step_id):
        self.step_ids.append(step_id)
        verdict = self.verdicts.pop(0)
        self.store.state = "verification" if verdict == "approved" else "changes_requested"
        return SimpleNamespace(verdict=verdict)


class Verifier:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.calls: list[dict] = []

    def verify(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(passed=self.passed, commands=[], summary="verification result")


class Checkpoint:
    def __init__(self, success: bool = True, retryable: bool = False) -> None:
        self.success = success
        self.retryable = retryable
        self.calls: list[dict] = []

    def checkpoint(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            success=self.success,
            retryable=self.retryable,
            commit_sha="b" * 40 if self.success else None,
            error="checkpoint failed" if not self.success else None,
        )


def orchestrator(store: StateStore, *, verdicts=None, verification_passes=True, checkpoint=None):
    planner = Planner(store)
    coder = Coder(store)
    reviewer = Reviewer(store, verdicts or ["approved"])
    verifier = Verifier(verification_passes)
    checkpoint = checkpoint or Checkpoint()
    unused_url = "postgresql:" + "//unused"
    config = OrchestratorConfig(
        database_url=unused_url,
        worker_id="orchestrator-test",
        poll_seconds=0.01,
        lease_seconds=60,
        repository_lock_seconds=60,
        auto_commit=True,
    )
    return (
        AgentOrchestrator(
            store=store,
            planner=planner,
            coder=coder,
            reviewer=reviewer,
            verifier=verifier,
            checkpoint=checkpoint,
            config=config,
        ),
        planner,
        coder,
        reviewer,
        verifier,
        checkpoint,
    )


def test_revision_cycle_keeps_same_step_then_replans_after_checkpoint():
    store = StateStore()
    subject, planner, coder, reviewer, verifier, checkpoint = orchestrator(
        store, verdicts=["changes_requested", "approved"]
    )

    actions = [subject.once().action for _ in range(8)]

    assert actions == [
        "planning",
        "coding",
        "review",
        "coding",
        "review",
        "verification",
        "checkpoint",
        "planning",
    ]
    assert [task.step_id for task in coder.tasks] == [7, 7]
    assert [task.attempt for task in coder.tasks] == [1, 2]
    assert reviewer.step_ids == [7, 7]
    assert verifier.calls and checkpoint.calls
    assert store.completed_steps == [7]
    assert planner.calls == 2
    assert store.state == "queued"  # Planner has produced the next task.


def test_failed_verification_returns_same_step_to_coder_without_checkpoint():
    store = StateStore(state="verification", job_status="verifying")
    subject, _planner, coder, _reviewer, verifier, checkpoint = orchestrator(
        store, verification_passes=False
    )

    result = subject.once()

    assert result.action == "verification"
    assert store.state == "changes_requested"
    assert verifier.calls
    assert checkpoint.calls == []

    subject.once()
    assert [task.step_id for task in coder.tasks] == [7]
    assert coder.tasks[0].attempt == 1


def test_paused_and_cancelled_jobs_never_claim_work_or_plan():
    for status in ("paused", "cancelled"):
        store = StateStore(state="queued", job_status=status)
        subject, planner, coder, reviewer, verifier, checkpoint = orchestrator(store)

        result = subject.once()

        assert result.action == "idle"
        assert planner.calls == 0
        assert coder.tasks == []
        assert reviewer.step_ids == []
        assert verifier.calls == []
        assert checkpoint.calls == []


def test_checkpoint_failure_requires_human_and_does_not_replan():
    store = StateStore(state="checkpoint", job_status="checkpointing")
    failed_checkpoint = Checkpoint(success=False, retryable=False)
    subject, planner, _coder, _reviewer, _verifier, checkpoint = orchestrator(
        store, checkpoint=failed_checkpoint
    )

    result = subject.once()

    assert result.action == "checkpoint"
    assert checkpoint.calls
    assert store.job_status == "needs_human"
    assert subject.once().action == "idle"
    assert planner.calls == 0
