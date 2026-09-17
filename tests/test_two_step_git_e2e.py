from __future__ import annotations
import subprocess
from pathlib import Path
from types import SimpleNamespace
from coder_agent.models import Status, Task
from orchestrator.checkpoint import CheckpointService
from orchestrator.config import OrchestratorConfig
from orchestrator.orchestrator import AgentOrchestrator
from orchestrator.verification import VerificationService

class DurableLifecycle:
    def __init__(self, repository):
        self.repository, self.branch = str(repository), "agents/two-step"
        self.steps, self.events, self.lock = [], [], None
    def recover_expired(self): return []
    def enforce_iteration_limit(self): return None
    def claim_verification(self, *_args):
        step = next((s for s in self.steps if s["status"] == "verification"), None)
        return dict(step) if step else None
    def record_verification(self, work, _worker, result):
        step = self.steps[work["id"] - 1]
        step["status"] = "checkpoint" if result.passed else "changes_requested"
        return step["status"]
    def claim_checkpoint(self, *_args):
        step = next((s for s in self.steps if s["status"] == "checkpoint"), None)
        return dict(step) if step else None
    def record_checkpoint(self, work, _worker, result):
        step = self.steps[work["id"] - 1]
        assert result.success, result.error
        step["status"], step["resulting_commit"] = "complete", result.commit_sha
        self.events.append(("checkpoint", step["id"], result.commit_sha))
        return "complete"
    def next_review_step(self):
        step = next((s for s in self.steps if s["status"] == "review"), None)
        return step["id"] if step else None
    def claim_coding(self, worker, *_args):
        step = next((s for s in self.steps if s["status"] in {"queued", "changes_requested"}), None)
        if not step: return None
        step["status"] = "running"; step["attempt_count"] += 1
        return Task(1, step["id"], self.repository, self.branch, step["title"],
                    ["The step file exists"], [], Status.RUNNING, step["attempt_count"])
    def finish_coding_handoff(self, step, _worker): return self.steps[step - 1]["status"]
    def finish_review_handoff(self, step): return self.steps[step - 1]["status"]
    def release_repository_lock(self, _repo, _owner): self.lock = None
    def heartbeat_repository_lock(self, *_args): return True
    def heartbeat_coding(self, *_args): return True
    def heartbeat_orchestration(self, *_args): return True
    def preexisting_files(self, _step): return []
    def record_model_invocation(self, **_payload): pass
    def step(self, step): return dict(self.steps[step - 1])

class Planner:
    last_job_id = 1
    router = SimpleNamespace(last_route=None)
    def __init__(self, store): self.store = store
    def plan_once(self):
        if any(s["status"] != "complete" for s in self.store.steps): return None
        if len(self.store.steps) >= 3: return SimpleNamespace(decision="complete")
        number = len(self.store.steps) + 1
        self.store.steps.append({"id":number,"job_id":1,"title":f"Roadmap step {number}",
            "repository":self.store.repository,"branch":self.store.branch,"status":"queued",
            "attempt_count":0,"starting_commit":git(Path(self.store.repository),"rev-parse","HEAD").strip(),
            "files_changed":[f"step{number}.txt"],"approved_files":[f"step{number}.txt"],
            "checkpoint_marker":f"autonomous-step:1:{number}:one"})
        return SimpleNamespace(decision="create_step")

class Coder:
    worker_id = "coder-e2e"; router = SimpleNamespace(last_route=None)
    def __init__(self, store): self.store = store
    def run_task(self, task):
        Path(task.repository, f"step{task.step_id}.txt").write_text(f"completed {task.step_id}\n")
        self.store.steps[task.step_id - 1]["status"] = "review"
        return SimpleNamespace(summary="implemented")

class Reviewer:
    router = SimpleNamespace(last_route=None)
    def __init__(self, store): self.store, self.review_counts = store, {}
    def review_once(self, step):
        count = self.review_counts.get(step, 0) + 1
        self.review_counts[step] = count
        # Deliberately exercise the same-step Reviewer -> Engineering revision
        # loop before approving the first package.
        if step == 1 and count == 1:
            self.store.steps[step - 1]["status"] = "changes_requested"
            self.store.events.append(("changes_requested", step))
            return SimpleNamespace(verdict="changes_requested")
        self.store.steps[step - 1]["status"] = "verification"
        self.store.events.append(("approved", step))
        return SimpleNamespace(verdict="approved")

def git(root, *args):
    done = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return done.stdout

def test_unattended_three_step_run_includes_revision_and_distinct_commits(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); git(root, "init", "-q", "-b", "agents/two-step")
    git(root, "config", "user.name", "Agent Test"); git(root, "config", "user.email", "agent@example.invalid")
    (root / "WORKER.md").write_text('## Verification\n- Tests: `python3 -c "print(1)"`\n')
    (root / "docs").mkdir(); (root / "docs/roadmap.md").write_text("- [ ] One\n- [ ] Two\n")
    git(root, "add", "."); git(root, "commit", "-qm", "initial")
    store = DurableLifecycle(root)
    config = OrchestratorConfig("postgresql://unused", "orch-e2e", poll_seconds=.01,
                                lease_seconds=60, repository_lock_seconds=60, auto_commit=True)
    subject = AgentOrchestrator(store=store, planner=Planner(store), coder=Coder(store),
        reviewer=Reviewer(store), verifier=VerificationService(), checkpoint=CheckpointService(), config=config)
    for _ in range(40):
        subject.once()
        if len([s for s in store.steps if s["status"] == "complete"]) == 3: break
    assert [s["status"] for s in store.steps] == ["complete", "complete", "complete"]
    assert len({s["resulting_commit"] for s in store.steps}) == 3
    assert [event[0] for event in store.events].count("approved") == 3
    assert [event[0] for event in store.events].count("changes_requested") == 1
    assert git(root, "log", "--format=%s", "-3").splitlines() == [
        "agent: Roadmap step 3", "agent: Roadmap step 2", "agent: Roadmap step 1"
    ]
