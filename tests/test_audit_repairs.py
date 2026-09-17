from __future__ import annotations

import json
from types import SimpleNamespace

from agent_core.models import Job, JobStatus
from orchestrator.config import OrchestratorConfig
from orchestrator.orchestrator import AgentOrchestrator
from planner_agent.planner import PlannerAgent
from reviewer_agent.reviewer import ReviewerAgent


class _PlanningStore:
    def __init__(self):
        self.job = Job(1, "goal", "/repo", "worker/x", JobStatus.PENDING)
        self.deferred = None

    def claim_job(self, *_args):
        self.job.status = JobStatus.PLANNING
        return self.job

    def context(self, *_args):
        return self.job, [], []

    def defer(self, job_id, worker_id, kind, detail, retry_seconds=20):
        self.deferred = (job_id, worker_id, kind, detail)


class _TimeoutInspector:
    def inspect(self, *_args):
        raise TimeoutError("git inspection timed out")


def test_planner_releases_lease_on_unexpected_inspection_failure():
    store = _PlanningStore()
    decision = PlannerAgent(store, object(), _TimeoutInspector(), "planner").plan_once()

    assert decision.job_status == "running"
    assert store.deferred[2] == "planner_internal_error"


def test_reviewer_context_budget_remains_valid_json():
    reviewer = ReviewerAgent(object(), object(), object(), "reviewer", max_context_chars=16_000)
    encoded = reviewer._context_json({
        "task": {"objective": "inspect"},
        "implementation": {"files_changed": ["app.py"], "command_results": []},
        "evidence": {"diff": "x" * 500_000, "documents": {"README.md": "y" * 100_000}},
        "prior_issues": [],
    })

    assert len(encoded) <= 16_000
    assert json.loads(encoded)["context_notice"]


class _ReviewCrashStore:
    def recover_expired(self): return []
    def enforce_iteration_limit(self): return None
    def claim_verification(self, *_args): return None
    def claim_checkpoint(self, *_args): return None
    def claim_coding(self, *_args): return None
    def next_review_step(self): return 7
    def step(self, step_id): return {"id": step_id, "job_id": 1}


class _CrashingReviewer:
    router = SimpleNamespace(last_route=None)
    worker_id = "reviewer"

    def __init__(self):
        self.abandoned = []

    def review_once(self, step_id):
        raise TimeoutError("review model timed out")

    def abandon(self, step_id, detail):
        self.abandoned.append((step_id, detail))


def test_orchestrator_releases_review_after_reviewer_crash():
    reviewer = _CrashingReviewer()
    subject = AgentOrchestrator(
        store=_ReviewCrashStore(), planner=SimpleNamespace(router=SimpleNamespace(last_route=None)),
        coder=SimpleNamespace(worker_id="coder"), reviewer=reviewer,
        verifier=object(), checkpoint=object(),
        config=OrchestratorConfig(database_url="postgresql://unused", worker_id="orch"),
    )

    result = subject.once()

    assert result.action == "review_crashed"
    assert reviewer.abandoned and reviewer.abandoned[0][0] == 7
