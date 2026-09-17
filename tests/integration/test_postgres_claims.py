from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg

from coder_agent.db import Store
from orchestrator.store import OrchestratorStore


def _seed_one_step(database_url: str) -> tuple[int, int]:
    with psycopg.connect(database_url) as connection:
        job_id = connection.execute(
            """
            INSERT INTO jobs (
                goal,
                repository,
                branch,
                status,
                priority
            )
            VALUES (
                'Integration claim test',
                '/tmp/integration-repository',
                'agents/integration-test',
                'running',
                0
            )
            RETURNING id
            """
        ).fetchone()[0]

        step_id = connection.execute(
            """
            INSERT INTO steps (
                job_id,
                sequence,
                repository,
                branch,
                title,
                objective,
                acceptance_criteria,
                constraints,
                assigned_agent,
                status
            )
            VALUES (
                %s,
                1,
                '/tmp/integration-repository',
                'agents/integration-test',
                'Claim exactly once',
                'Prove concurrent Engineering workers cannot double-claim.',
                '[]'::jsonb,
                '[]'::jsonb,
                'engineering-agent',
                'queued'
            )
            RETURNING id
            """,
            (job_id,),
        ).fetchone()[0]

    return job_id, step_id


def test_two_engineers_cannot_claim_same_step(postgres_database):
    OrchestratorStore(postgres_database).migrate()
    job_id, step_id = _seed_one_step(postgres_database)

    store = Store(postgres_database)

    def claim(worker_id: str):
        return store.claim(worker_id, lease_seconds=60)

    # Start two real PostgreSQL transactions concurrently.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, "engineer-1"),
            executor.submit(claim, "engineer-2"),
        ]
        results = [future.result() for future in futures]

    winners = [result for result in results if result is not None]
    losers = [result for result in results if result is None]

    assert len(winners) == 1
    assert len(losers) == 1

    winner = winners[0]
    assert winner.job_id == job_id
    assert winner.step_id == step_id
    assert winner.attempt == 1

    with psycopg.connect(postgres_database) as connection:
        row = connection.execute(
            """
            SELECT status, worker_id, attempt_count
            FROM steps
            WHERE id = %s
            """,
            (step_id,),
        ).fetchone()

    assert row[0] == "running"
    assert row[1] in {"engineer-1", "engineer-2"}
    assert row[2] == 1
