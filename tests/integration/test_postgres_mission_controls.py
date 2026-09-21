from __future__ import annotations

import pytest

from orchestrator.store import OrchestratorStore


def _mission_with_packages(store: OrchestratorStore) -> tuple[int, int, int]:
    mission_id = store.create_mission(
        "Exercise durable mission controls",
        "/tmp/integration-repository",
        "agents/integration-test",
    )
    first = store.create_work_package(
        mission_id,
        "Build the foundation",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["Foundation is complete"],
    )
    second = store.create_work_package(
        mission_id,
        "Build the dependent feature",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["Feature uses the foundation"],
        dependencies=[first],
    )
    return mission_id, first, second


def test_pause_resume_and_dependency_projection_survive_reconnect(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    store.migrate()  # additive migration remains safe across service restarts
    mission_id, first, second = _mission_with_packages(store)

    paused = store.control_mission(mission_id, "pause")
    repeated = store.control_mission(mission_id, "pause")

    assert paused["status"] == "paused"
    assert repeated["idempotent"] is True
    assert store.claim_work_package("engineer-while-paused", 300) is None

    # A new store represents a process/systemd restart. Durable mission and
    # package state, including dependency projection, must be reconstructed.
    restarted = OrchestratorStore(postgres_database)
    detail = restarted.mission_detail(mission_id)
    assert detail is not None and detail["status"] == "paused"
    by_id = {package["id"]: package for package in detail["packages"]}
    assert by_id[second]["unmet_dependencies"] == [first]
    assert by_id[second]["dependency_state"] == "waiting"

    resumed = restarted.control_mission(mission_id, "resume")
    assert resumed["status"] == "active"
    assert restarted.claim_work_package("engineer-after-resume", 300)["id"] == first


def test_pause_preserves_inflight_ownership_until_safe_boundary(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id, first, _second = _mission_with_packages(store)
    claimed = store.claim_engineering_package(
        "engineer-1", 300, 300, max_concurrent_per_repository=1
    )
    assert claimed is not None and claimed["package"]["id"] == first

    result = store.control_mission(mission_id, "pause")
    assert result["in_flight_packages"] == [first]

    with store.connect() as connection:
        step = connection.execute(
            "SELECT status,worker_id,lease_expires_at FROM steps WHERE id=%s",
            (claimed["task"].step_id,),
        ).fetchone()
        package = connection.execute(
            "SELECT status,worker_id,lease_expires_at FROM work_packages WHERE id=%s",
            (first,),
        ).fetchone()
        lock = connection.execute(
            "SELECT owner FROM repository_locks WHERE repository=%s",
            ("/tmp/integration-repository",),
        ).fetchone()

    assert step["status"] == "running"
    assert step["worker_id"] == "engineer-1"
    assert step["lease_expires_at"] is not None
    assert package["status"] == "ready"
    assert package["worker_id"] is None
    assert lock is not None


def test_cancel_is_idempotent_and_restart_reconciles_expired_inflight_work(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id, first, _second = _mission_with_packages(store)
    claimed = store.claim_engineering_package("engineer-1", 300, 300)
    assert claimed is not None
    store.start_engineering_session(
        claimed["task"].job_id,
        claimed["task"].step_id,
        "engineer-1",
        "abc1234",
    )

    cancelled = store.control_mission(mission_id, "cancel")
    repeated = store.control_mission(mission_id, "cancel")
    assert cancelled["status"] == "cancelled"
    assert repeated["idempotent"] is True
    assert store.claim_work_package("engineer-after-cancel", 300) is None

    with store.connect() as connection:
        connection.execute(
            "UPDATE steps SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (claimed["task"].step_id,),
        )
        connection.execute(
            "UPDATE repository_locks SET lease_expires_at=now()-interval '1 second'"
        )

    restarted = OrchestratorStore(postgres_database)
    reconciled = restarted.reconcile_mission_controls()
    assert claimed["task"].step_id in reconciled["cancelled_steps"]

    with restarted.connect() as connection:
        mission = connection.execute(
            "SELECT status FROM missions WHERE id=%s", (mission_id,)
        ).fetchone()
        job = connection.execute(
            "SELECT status FROM jobs WHERE id=%s", (claimed["task"].job_id,)
        ).fetchone()
        step = connection.execute(
            "SELECT status,worker_id,lease_expires_at FROM steps WHERE id=%s",
            (claimed["task"].step_id,),
        ).fetchone()
        session_count = connection.execute(
            "SELECT count(*) AS n FROM engineering_sessions WHERE step_id=%s",
            (claimed["task"].step_id,),
        ).fetchone()["n"]

    assert mission["status"] == "cancelled"
    assert job["status"] == "cancelled"
    assert step["status"] == "cancelled"
    assert step["worker_id"] is None and step["lease_expires_at"] is None
    assert session_count == 1  # Control never deletes in-flight session evidence.


def test_invalid_mission_transition_and_operation_history_are_durable(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id, _first, _second = _mission_with_packages(store)

    operation_id = store.begin_control_operation(
        None, "resume", "integration-test", mission_id=mission_id
    )
    store.update_control_operation(operation_id, "accepted")
    with pytest.raises(ValueError, match="cannot resume"):
        store.control_mission(mission_id, "resume")
    store.update_control_operation(
        operation_id, "rejected", error="cannot resume an active mission"
    )

    operation = store.control_operation(operation_id)
    assert operation is not None
    assert operation["mission_id"] == mission_id
    assert operation["status"] == "rejected"
    assert [entry["status"] for entry in operation["history"]] == [
        "submitted", "accepted", "rejected"
    ]
