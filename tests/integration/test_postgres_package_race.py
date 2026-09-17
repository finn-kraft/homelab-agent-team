from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from orchestrator.store import OrchestratorStore


WORKERS = 20


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
                "Concurrent package claim test",
                "/tmp/integration-repository",
                "agents/integration-test",
            ),
        ).fetchone()["id"]

    package_id = store.create_work_package(
        mission_id,
        "Exactly one Engineering worker may claim this package.",
        "/tmp/integration-repository",
        "agents/integration-test",
        ["Exactly one worker owns the package."],
        constraints=["Never duplicate package execution."],
    )

    return mission_id, package_id


def test_many_engineers_cannot_double_claim_one_package(
    postgres_database,
):
    store = OrchestratorStore(postgres_database)
    store.migrate()

    mission_id, package_id = _seed_package(store)

    # Hold every thread here until all workers are ready. This makes
    # their PostgreSQL claims happen as close together as practical.
    barrier = Barrier(WORKERS)

    def race(worker_number: int):
        worker_id = f"engineer-{worker_number}"

        barrier.wait()

        result = store.claim_work_package(
            worker_id,
            lease_seconds=300,
        )

        return worker_id, result

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [
            executor.submit(race, number)
            for number in range(1, WORKERS + 1)
        ]

        results = [
            future.result()
            for future in futures
        ]

    winners = [
        (worker_id, package)
        for worker_id, package in results
        if package is not None
    ]

    losers = [
        worker_id
        for worker_id, package in results
        if package is None
    ]

    assert len(winners) == 1
    assert len(losers) == WORKERS - 1

    winner_id, winner_package = winners[0]

    assert winner_package["id"] == package_id
    assert winner_package["mission_id"] == mission_id

    packages = store.list_work_packages(mission_id)

    assert len(packages) == 1

    durable = packages[0]

    assert durable["id"] == package_id
    assert durable["status"] == "engineering"
    assert durable["worker_id"] == winner_id
    assert durable["lease_expires_at"] is not None
