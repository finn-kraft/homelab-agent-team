from __future__ import annotations

import pytest

from orchestrator.store import OrchestratorStore


def _seed_request(store: OrchestratorStore) -> tuple[int, int, int, int]:
    mission_id = store.create_mission(
        "Exercise durable human request history",
        "/tmp/integration-repository",
        "agents/integration-test",
    )
    package_id = store.create_work_package(
        mission_id,
        "Ask for an operator decision",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["The decision lifecycle is reconstructable"],
    )
    package = store.list_work_packages(mission_id)[0]
    with store.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status='needs_human',current_phase='needs_human' WHERE id=%s",
            (package["job_id"],),
        )
        connection.execute(
            "UPDATE steps SET status='needs_human' WHERE id=%s",
            (package["step_id"],),
        )
    request_id = store.enqueue_human_request(
        mission_id=mission_id,
        job_id=package["job_id"],
        step_id=package["step_id"],
        kind="product-decision",
        question="Choose an API mode; token=do-not-store-this",
        context={"evidence": "postgresql://user:pass@db/work", "candidate": "strict"},
    )
    return mission_id, package["job_id"], package["step_id"], request_id


def test_human_request_lifecycle_is_append_only_and_restart_safe(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id, job_id, step_id, request_id = _seed_request(store)

    duplicate_id = store.enqueue_human_request(
        mission_id=mission_id,
        job_id=job_id,
        step_id=step_id,
        kind="product-decision",
        question="Updated evidence must be appended, not overwrite the question",
        context={"new_evidence": "operator requested strict mode"},
    )
    assert duplicate_id == request_id

    store.claim_human_request(request_id, "operator:finn")
    with pytest.raises(ValueError, match="owned"):
        store.claim_human_request(request_id, "operator:someone-else")
    store.answer_human_request(request_id, "Use strict mode", answered_by="operator:finn")
    store.record_human_request_outcome(
        request_id, "step_completed", {"step_id": step_id, "commit_sha": "abc1234"}
    )

    restarted = OrchestratorStore(postgres_database)
    history = restarted.list_human_queue("resolved", limit=20)
    assert len(history) == 1
    request = history[0]
    assert request["id"] == request_id
    assert request["owner"] == "operator:finn"
    assert request["answer"] == "Use strict mode"
    assert request["resolution"] == "answered"
    assert request["outcome"]["type"] == "step_completed"
    assert "do-not-store-this" not in request["question"]
    assert "user:pass" not in str(request["context"])
    assert [event["event_type"] for event in request["history"]] == [
        "created",
        "evidence_observed",
        "owned",
        "answered",
        "resolved",
        "outcome_recorded",
        "outcome_recorded",
    ]
    assert request["history"][-2]["evidence"]["type"] == "workflow_resumed"
    assert request["history"][-1]["evidence"]["type"] == "step_completed"

    with restarted.connect() as connection:
        job = connection.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()
        step = connection.execute("SELECT status FROM steps WHERE id=%s", (step_id,)).fetchone()
    assert job["status"] == "running"
    assert step["status"] == "changes_requested"


def test_cancelled_request_retains_creation_evidence_and_resolution(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id, _job_id, _step_id, request_id = _seed_request(store)

    store.control_mission(mission_id, "cancel")

    request = store.human_request(request_id)
    assert request is not None
    assert request["status"] == "cancelled"
    assert request["resolution"] == "cancelled"
    assert [event["event_type"] for event in request["history"]] == [
        "created", "resolved"
    ]
