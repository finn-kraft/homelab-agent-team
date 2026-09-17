import pytest
from types import SimpleNamespace

from coder_agent import CoderAgent, EngineeringAgent
from coder_agent.models import Status, Task, WorkPackage
from orchestrator.config import OrchestratorConfig
from orchestrator.orchestrator import AgentOrchestrator


def test_engineering_agent_is_canonical_with_v1_compatibility_alias():
    assert EngineeringAgent is CoderAgent
    assert hasattr(EngineeringAgent, "run_work_package")
    assert hasattr(EngineeringAgent, "run_package")


def test_orchestrator_builds_engineering_package_on_isolated_step_checkout():
    class Store:
        def work_package_for_step(self, _step_id):
            return {
                "id": 4,
                "job_id": 9,
                "repository": "/home/finn/work/source",
                "branch": "agents/pkg-4",
                "objective": "Improve reporting",
                "acceptance_criteria": ["tests pass"],
                "constraints": [],
                "dependencies": [],
            }

    subject = AgentOrchestrator(
        store=Store(), planner=object(),
        engineer=SimpleNamespace(worker_id="engineering-1"),
        reviewer=object(), verifier=object(), checkpoint=object(),
        config=OrchestratorConfig(database_url="postgresql://unused", worker_id="orch"),
    )
    task = Task(9, 12, "/tmp/agent-worktrees/pkg-4", "agents/pkg-4",
                "Improve reporting", ["tests pass"], status=Status.RUNNING)

    package = subject._work_package_for_step(task)

    assert package.repository == task.repository
    assert package.branch == task.branch
    assert package.id == 4 and package.job_id == 9


def test_work_package_preserves_reviewer_feedback_in_task_transport():
    package = WorkPackage(4, 9, "/tmp/repo", "agents/pkg-4", "Improve reporting",
                          ["tests pass"], reviewer_feedback={"issue": "cover edge case"})
    task = package.as_task(12, attempt=2)
    assert task.job_id == 9 and task.step_id == 12
    assert task.attempt == 2
    assert task.reviewer_feedback == {"issue": "cover edge case"}


def test_work_package_requires_durable_step_for_execution():
    class Stub: pass
    # The boundary intentionally refuses to invent a Step identity.
    from coder_agent.agent import CoderAgent
    with pytest.raises(ValueError):
        CoderAgent(Stub(), Stub(), "worker", []).run_package(
            WorkPackage(1, 1, "/tmp/repo", "main", "objective", ["criteria"])
        )
