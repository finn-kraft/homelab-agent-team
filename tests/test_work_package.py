import pytest

from coder_agent.models import WorkPackage


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
