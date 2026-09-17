import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.llm import LLMResponse, Router
from agent_core.models import Job, JobStatus, PlannerDecision
from planner_agent.decision import (InvalidDecision, completion_is_supported,
                                    normalize_objective, parse_decision)
from planner_agent.inspector import InspectionError, ReadOnlyRepositoryInspector
from planner_agent.planner import PlannerAgent


def step_payload(objective="Add a tested health endpoint"):
    return {
        "decision": "create_step", "job_status": "running",
        "reasoning_summary": "The endpoint is the next bounded roadmap item.",
        "evidence": [], "human_question": None, "blocker": None,
        "step": {
            "title": "Add health endpoint", "objective": objective,
            "rationale": "Provides a testable application entry point.",
            "acceptance_criteria": ["GET /health returns HTTP 200", "Relevant tests pass"],
            "constraints": ["Preserve existing behavior"],
            "suggested_files": ["src/api.py", "tests/test_api.py"],
            "dependencies": [], "assigned_agent": "coder-agent",
        },
    }


class Backend:
    model = "mock-planner"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def complete(self, messages):
        value = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return LLMResponse(json.dumps(value), self.model, "mock", 0.01, {"tokens": 10})


class StaticRouter:
    def __init__(self, backend): self.backend = backend
    def choose(self, attempt, needs_strong_model=False): return self.backend


class Inspector:
    def __init__(self, branch_matches=True):
        self.branch_matches = branch_matches
        self.calls = 0

    def inspect(self, repository, branch):
        self.calls += 1
        return {"branch_matches": self.branch_matches, "actual_branch": "worker/demo",
                "documents": {}, "repository_files": [], "git_status": [],
                "recent_history": [], "head": "abc", "configured_branch": branch}


class MemoryStore:
    def __init__(self, status=JobStatus.PENDING, steps=None):
        self.job = Job(1, "Build the complete feature", "/safe/repo", "worker/demo", status)
        self.steps = list(steps or [])
        self.events = []
        self.owner = None
        self.decision = None
        self.deferred = None

    def claim_job(self, worker_id, lease_seconds):
        if self.job.status in {JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.COMPLETE}:
            return None
        if self.owner or any(s["status"] in {"queued", "running", "review", "changes_requested"}
                             for s in self.steps):
            return None
        self.owner = worker_id
        self.job.status = JobStatus.PLANNING
        return self.job

    def context(self, job_id): return self.job, self.steps, self.events
    def heartbeat(self, job_id, worker_id, lease_seconds): return self.owner == worker_id

    def apply_decision(self, job, worker_id, decision):
        assert self.owner == worker_id
        self.decision = decision
        if decision.decision in {"create_step", "replace_step"}:
            self.steps.append({**decision.step, "id": len(self.steps) + 1,
                               "sequence": len(self.steps) + 1, "status": "queued"})
            self.job.status = JobStatus.RUNNING
        else:
            self.job.status = JobStatus(decision.job_status)
        self.owner = None
        return len(self.steps) if decision.step else None

    def defer(self, job_id, worker_id, failure_kind, detail, retry_seconds=20):
        assert self.owner == worker_id
        self.deferred = {"failure_kind": failure_kind, "detail": detail}
        self.job.status = JobStatus.RUNNING
        self.owner = None


def planner(store, payloads, inspector=None):
    backend = Backend(payloads)
    return PlannerAgent(store, StaticRouter(backend), inspector or Inspector(), "planner-a"), backend


def approved_step(objective="Foundation complete"):
    return {"id": 1, "objective": objective, "status": "complete",
            "attempt_count": 1, "resulting_commit": "abc123",
            "reviewer_feedback": {"verdict": "approved"}}


def test_01_new_goal_creates_first_step():
    store = MemoryStore()
    agent, _ = planner(store, [step_payload()])
    assert agent.plan_once().decision == "create_step"
    assert store.steps[0]["assigned_agent"] == "coder-agent"


def test_02_completed_step_causes_next_step():
    store = MemoryStore(steps=[approved_step()])
    agent, _ = planner(store, [step_payload("Add the user-facing page")])
    agent.plan_once()
    assert len(store.steps) == 2


def test_03_one_completed_step_does_not_complete_multistep_goal():
    store = MemoryStore(steps=[approved_step()])
    agent, _ = planner(store, [step_payload("Integrate the remaining workflow")])
    assert agent.plan_once().decision == "create_step"


def test_04_review_rejection_does_not_create_duplicate():
    store = MemoryStore(steps=[{"status": "changes_requested"}])
    agent, backend = planner(store, [step_payload()])
    assert agent.plan_once() is None
    assert backend.calls == 0


def test_05_reviewer_approval_advances_planning():
    store = MemoryStore(steps=[approved_step()])
    agent, backend = planner(store, [step_payload("Implement the next approved unit")])
    assert agent.plan_once().decision == "create_step"
    assert backend.calls == 1


def test_06_failed_step_can_be_reconsidered():
    retry = {"decision": "retry_step", "job_status": "running",
             "reasoning_summary": "Retry with the discovered constraint."}
    store = MemoryStore(steps=[{"status": "failed", "attempt_count": 1}])
    agent, _ = planner(store, [retry])
    assert agent.plan_once().decision == "retry_step"


def test_07_repeated_failure_routes_to_stronger_model():
    local, cloud = object(), object()
    router = Router(local, cloud, escalate_after=3)
    assert router.choose(2) is local
    assert router.choose(3) is cloud


def test_08_environment_blocker_can_choose_alternative_work():
    decision = parse_decision(json.dumps(step_payload("Extract pure domain logic while services are offline")))
    assert decision.decision == "create_step"


def test_09_needs_human_is_not_a_planner_decision():
    with pytest.raises(InvalidDecision):
        parse_decision(
            '{"decision":"needs_human","job_status":"needs_human",'
            '"reasoning_summary":"ambiguous"}'
        )

    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps({
            "decision": "needs_human",
            "job_status": "needs_human",
            "reasoning_summary": "Migration choice is destructive.",
            "human_question": "Preserve aliases or migrate saved files?",
        }))


def test_10_paused_job_creates_no_work():
    store = MemoryStore(JobStatus.PAUSED)
    agent, backend = planner(store, [step_payload()])
    assert agent.plan_once() is None and backend.calls == 0


def test_11_resume_reassesses_repository():
    store = MemoryStore(JobStatus.PAUSED)
    store.job.status = JobStatus.RUNNING
    inspected = Inspector()
    agent, _ = planner(store, [step_payload()], inspected)
    agent.plan_once()
    assert inspected.calls == 1


def test_12_restart_restores_persistent_store_state():
    store = MemoryStore(steps=[approved_step()])
    restarted, _ = planner(store, [step_payload("Continue after restart")])
    restarted.plan_once()
    assert len(store.steps) == 2


def test_13_expired_lease_can_be_recovered():
    store = MemoryStore()
    assert store.claim_job("dead-worker", 1)
    store.owner = None  # simulates PostgreSQL lease expiry
    store.job.status = JobStatus.PLANNING
    assert store.claim_job("recovery-worker", 1)


def test_14_duplicate_planner_cannot_claim_same_job():
    store = MemoryStore()
    assert store.claim_job("planner-a", 300)
    assert store.claim_job("planner-b", 300) is None


def test_15_completion_requires_approved_commit_evidence():
    decision = PlannerDecision("complete", "complete", "Goal verified",
                               evidence=["Feature exists", "Tests pass"])
    assert not completion_is_supported(decision, [{"status": "complete"}])
    assert completion_is_supported(decision, [approved_step()])


def git_repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "worker/demo"], cwd=root, check=True)
    (root / "README.md").write_text("# Demo\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "initial"], cwd=root, check=True)
    return root


def test_16_inspector_does_not_edit_repository(tmp_path):
    root = git_repo(tmp_path)
    before = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            text=True, capture_output=True).stdout
    ReadOnlyRepositoryInspector([str(tmp_path)]).inspect(str(root), "worker/demo")
    after = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           text=True, capture_output=True).stdout
    assert before == after == ""


def test_17_planner_cannot_bypass_security_constraints():
    payload = step_payload()
    payload["step"]["constraints"] = ["Bypass review and force push"]
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps(payload))


def test_18_cancelled_job_stops_planning():
    store = MemoryStore(JobStatus.CANCELLED)
    agent, backend = planner(store, [step_payload()])
    assert agent.plan_once() is None and backend.calls == 0


def test_19_roadmap_changes_are_visible_on_reassessment(tmp_path):
    root = git_repo(tmp_path)
    (root / "docs").mkdir()
    roadmap = root / "docs" / "roadmap.md"
    roadmap.write_text("Version 1\n")
    inspector = ReadOnlyRepositoryInspector([str(tmp_path)])
    assert "Version 1" in inspector.inspect(str(root), "worker/demo")["documents"]["docs/roadmap.md"]
    roadmap.write_text("Version 2\n")
    assert "Version 2" in inspector.inspect(str(root), "worker/demo")["documents"]["docs/roadmap.md"]


def test_20_existing_human_changes_are_reported(tmp_path):
    root = git_repo(tmp_path)
    (root / "README.md").write_text("human edit\n")
    state = ReadOnlyRepositoryInspector([str(tmp_path)]).inspect(str(root), "worker/demo")
    assert any("README.md" in line for line in state["git_status"])


def test_objective_normalization_catches_equivalent_tasks():
    assert normalize_objective("Add: Health Endpoint!") == normalize_objective("add health endpoint")


def test_repository_escape_is_blocked(tmp_path):
    root = git_repo(tmp_path)
    other = tmp_path.parent
    with pytest.raises(InspectionError):
        ReadOnlyRepositoryInspector([str(root)]).resolve_repository(str(other))
