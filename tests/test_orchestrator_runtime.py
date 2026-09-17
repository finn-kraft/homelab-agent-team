from __future__ import annotations

import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coder_agent.models import Status, Task
from orchestrator.cli import _install_stop_handlers
from orchestrator.config import OrchestratorConfig
from orchestrator.orchestrator import AdvanceResult, AgentOrchestrator


def config() -> OrchestratorConfig:
    unused_url = "postgresql:" + "//unused"
    return OrchestratorConfig(
        database_url=unused_url, worker_id="orch-test",
        poll_seconds=0.01, lease_seconds=60, repository_lock_seconds=60,
    )


class IdleStore:
    def recover_expired(self): return []
    def enforce_iteration_limit(self): return None
    def claim_verification(self, *args): return None
    def claim_checkpoint(self, *args): return None
    def next_review_step(self): return None
    def claim_coding(self, *args): return None


class IdlePlanner:
    last_job_id = None
    router = SimpleNamespace(last_route=None)
    def plan_once(self): return None


class IdleOrchestrator(AgentOrchestrator):
    def __init__(self):
        super().__init__(
            store=IdleStore(), planner=IdlePlanner(), coder=SimpleNamespace(worker_id="coder"),
            reviewer=object(), verifier=object(), checkpoint=object(), config=config(),
        )
        self.calls = 0

    def once(self):
        self.calls += 1
        return AdvanceResult("idle")


def test_idle_run_waits_instead_of_busy_spinning():
    subject = IdleOrchestrator()
    thread = threading.Thread(target=subject.run)
    thread.start()
    time.sleep(0.045)
    subject.request_stop()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert 1 <= subject.calls <= 7


def test_sigterm_handler_requests_graceful_stop():
    subject = IdleOrchestrator()
    handlers = {}

    with patch("signal.signal", side_effect=lambda kind, handler: handlers.setdefault(kind, handler)):
        _install_stop_handlers(subject)
    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert subject._stop_requested.is_set()


class CrashStore(IdleStore):
    def __init__(self, repository: str):
        self.repository = repository
        self.claimed = False
        self.released = []

    def claim_coding(self, *args):
        if self.claimed: return None
        self.claimed = True
        return Task(1, 7, self.repository, "agents/work", "work", ["done"], [], Status.RUNNING, 1)

    def release_repository_lock(self, repository, owner): self.released.append((repository, owner))
    def heartbeat_repository_lock(self, *args): return True
    def heartbeat_coding(self, *args): return True


class CrashingCoder:
    worker_id = "coder-crash"
    router = SimpleNamespace(last_route=None)
    def run_task(self, task): raise RuntimeError("simulated worker crash")


def test_coder_crash_releases_repository_lock(tmp_path):
    store = CrashStore(str(tmp_path))
    subject = AgentOrchestrator(
        store=store, planner=IdlePlanner(), coder=CrashingCoder(), reviewer=object(),
        verifier=object(), checkpoint=object(), config=config(),
    )

    result = subject.once()

    assert result.action == "engineering_crashed"
    assert store.released == [(str(tmp_path), "coder-crash:engineering:7")]


class RecoveryStore(IdleStore):
    def __init__(self, stale): self.stale=stale; self.requeued=[]; self.blocked=[]
    def recover_expired(self):
        value, self.stale = self.stale, []
        return value
    def safely_requeue_abandoned_coding(self, step_id, evidence): self.requeued.append(step_id)
    def block_abandoned_coding(self, step_id, evidence): self.blocked.append((step_id,evidence))


class PlanningRecoveryStore(IdleStore):
    def recover_expired_planning(self):
        return [9]


def test_expired_planner_lease_is_released_for_reclaim():
    result = recovery_subject(PlanningRecoveryStore()).once()
    assert result.action == "planning_recovered"
    assert result.job_id == 9


def git_repo(tmp_path: Path) -> Path:
    root=tmp_path/"repo"; root.mkdir()
    subprocess.run(["git","init","-q","-b","agents/work"],cwd=root,check=True)
    subprocess.run(["git","config","user.name","Test"],cwd=root,check=True)
    subprocess.run(["git","config","user.email","test@example.invalid"],cwd=root,check=True)
    (root/"app.py").write_text("value=1\n")
    subprocess.run(["git","add","app.py"],cwd=root,check=True)
    subprocess.run(["git","commit","-qm","start"],cwd=root,check=True)
    return root


def recovery_subject(store):
    return AgentOrchestrator(
        store=store, planner=IdlePlanner(), coder=SimpleNamespace(worker_id="coder"),
        reviewer=object(), verifier=object(), checkpoint=object(), config=config(),
    )


def test_expired_clean_coder_lease_is_requeued(tmp_path):
    root=git_repo(tmp_path)
    stale=[{"id":7,"job_id":1,"repository":str(root),"status":"running"}]
    store=RecoveryStore(stale)

    assert recovery_subject(store).once().action == "engineering_requeued"
    assert store.requeued == [7]


def test_expired_dirty_coder_lease_blocks_for_safe_reconciliation(tmp_path):
    root=git_repo(tmp_path); (root/"app.py").write_text("value=2\n")
    stale=[{"id":7,"job_id":1,"repository":str(root),"status":"running"}]
    store=RecoveryStore(stale)

    assert recovery_subject(store).once().action == "engineering_blocked"
    assert store.blocked and "unclassified" in store.blocked[0][1]
