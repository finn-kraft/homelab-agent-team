from __future__ import annotations

from orchestrator.store import OrchestratorStore


def _seed_mission_and_package(
    store: OrchestratorStore,
) -> tuple[int, int, int, int]:
    with store.connect() as connection:
        mission_id = connection.execute(
            """
            INSERT INTO missions (
                goal,
                repository,
                branch,
                status
            )
            VALUES (%s, %s, %s, 'active')
            RETURNING id
            """,
            (
                "Prove package crash recovery",
                "/tmp/integration-repository",
                "agents/integration-test",
            ),
        ).fetchone()["id"]

    package_id = store.create_work_package(
        mission_id,
        "Recover an Engineering package after its worker crashes.",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["The same package is reclaimed after lease expiry."],
        constraints=["Do not create duplicate packages."],
    )

    package = store.list_work_packages(mission_id)[0]

    return (
        mission_id,
        package_id,
        package["job_id"],
        package["step_id"],
    )


def test_expired_package_is_recovered_and_reclaimed(
    postgres_database,
):
    store = OrchestratorStore(postgres_database)
    store.migrate()

    mission_id, package_id, job_id, step_id = (
        _seed_mission_and_package(store)
    )

    first = store.claim_work_package(
        "engineer-1",
        lease_seconds=300,
    )

    assert first is not None
    assert first["id"] == package_id
    assert first["mission_id"] == mission_id

    # Persist real durable Engineering session state before simulating
    # the worker crash.
    session_id = store.start_engineering_session(
        job_id,
        step_id,
        "engineer-1",
        "abc1234",
    )

    with store.connect() as connection:
        connection.execute(
            """
            UPDATE engineering_sessions
            SET current_model_tier='local',
                current_problem='debug failing projection test',
                progress_score=0.5,
                stagnation_count=2,
                turn_count=11,
                last_successful_action='write',
                last_test_result=%s::jsonb
            WHERE id=%s
            """,
            ('{"passed":4,"failed":1}', session_id),
        )

        # Failure injection: engineer-1 disappears without releasing
        # its package lease.
        connection.execute(
            """
            UPDATE work_packages
            SET lease_expires_at=now() - interval '1 minute'
            WHERE id=%s
            """,
            (package_id,),
        )

    recovered = store.recover_work_packages()

    assert recovered == [package_id]

    with store.connect() as connection:
        package = connection.execute(
            """
            SELECT status,worker_id,lease_expires_at
            FROM work_packages
            WHERE id=%s
            """,
            (package_id,),
        ).fetchone()

    assert package["status"] == "ready"
    assert package["worker_id"] is None
    assert package["lease_expires_at"] is None

    # Durable session state must survive package recovery.
    session = store.engineering_session_state(
        job_id,
        step_id,
    )

    assert session is not None
    assert session["id"] == session_id
    assert session["worker_id"] == "engineer-1"
    assert session["current_model_tier"] == "local"
    assert session["current_problem"] == (
        "debug failing projection test"
    )
    assert session["stagnation_count"] == 2
    assert session["turn_count"] == 11
    assert session["last_successful_action"] == "write"
    assert session["last_test_result"] == {
        "passed": 4,
        "failed": 1,
    }

    # A different worker must reclaim the same package, not create
    # replacement work.
    second = store.claim_work_package(
        "engineer-2",
        lease_seconds=300,
    )

    assert second is not None
    assert second["id"] == package_id

    packages = store.list_work_packages(mission_id)

    assert len(packages) == 1
    assert packages[0]["id"] == package_id
    assert packages[0]["status"] == "engineering"
    assert packages[0]["worker_id"] == "engineer-2"

    # The durable Engineering session still belongs to the same
    # package/job/step after reclamation.
    resumed_session = store.resume_engineering_session(
        job_id,
        step_id,
    )

    assert resumed_session == session_id
