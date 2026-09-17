from __future__ import annotations

import psycopg
import pytest

from orchestrator.store import OrchestratorStore


def _seed_package(store: OrchestratorStore) -> tuple[int, int]:
    with store.connect() as connection:
        mission_id = connection.execute(
            """
            INSERT INTO missions (
                goal, repository, branch, status
            )
            VALUES (%s, %s, %s, 'active')
            RETURNING id
            """,
            (
                "Database outage recovery test",
                "/tmp/integration-repository",
                "agents/integration-test",
            ),
        ).fetchone()["id"]

    package_id = store.create_work_package(
        mission_id,
        "Remain consistent when PostgreSQL disappears mid-transaction.",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["Interrupted transactions are rolled back."],
        constraints=["Package must remain safely claimable."],
    )

    return mission_id, package_id


def test_database_disconnect_rolls_back_partial_transition_and_recovers(
    postgres_database,
    monkeypatch,
):
    store = OrchestratorStore(postgres_database)
    store.migrate()

    mission_id, package_id = _seed_package(store)

    # Open two independent real PostgreSQL connections:
    # one represents the workflow transaction, the other injects failure.
    victim = psycopg.connect(postgres_database)
    killer = psycopg.connect(postgres_database, autocommit=True)

    try:
        backend_pid = victim.execute(
            "SELECT pg_backend_pid()"
        ).fetchone()[0]

        # Begin a real state transition but deliberately do NOT commit it.
        victim.execute(
            """
            UPDATE work_packages
            SET status='engineering',
                worker_id='engineer-crashed',
                updated_at=now()
            WHERE id=%s
            """,
            (package_id,),
        )

        # Failure injection: PostgreSQL kills the backend while its
        # transaction contains the uncommitted workflow transition.
        terminated = killer.execute(
            "SELECT pg_terminate_backend(%s)",
            (backend_pid,),
        ).fetchone()[0]

        assert terminated is True

        # The dead connection must no longer be usable.
        with pytest.raises(psycopg.Error):
            victim.execute("SELECT 1")

    finally:
        victim.close()
        killer.close()

    # A fresh connection represents the application recovering after
    # database connectivity returns.
    with store.connect() as connection:
        package = connection.execute(
            """
            SELECT status,worker_id
            FROM work_packages
            WHERE id=%s
            """,
            (package_id,),
        ).fetchone()

    # PostgreSQL atomicity guarantee: the interrupted transaction must
    # have disappeared completely.
    assert package["status"] == "ready"
    assert package["worker_id"] is None

    # Normal workflow can immediately continue after reconnection.
    claimed = store.claim_work_package(
        "engineer-recovery",
        lease_seconds=300,
    )

    assert claimed is not None
    assert claimed["id"] == package_id

    packages = store.list_work_packages(mission_id)

    assert len(packages) == 1
    assert packages[0]["status"] == "engineering"
    assert packages[0]["worker_id"] == "engineer-recovery"
