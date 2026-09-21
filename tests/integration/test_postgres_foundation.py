from __future__ import annotations

import psycopg

from orchestrator.store import MIGRATION_VERSION, OrchestratorStore


def test_real_postgres_migration_reaches_ready_state(postgres_database):
    store = OrchestratorStore(postgres_database)

    before = store.schema_readiness()
    assert before["ready"] is False
    assert before["migration_required"] is True

    store.migrate()

    after = store.schema_readiness()
    print("\nREADINESS AFTER MIGRATION:", after)
    assert after["ready"] is True
    assert after["migration_required"] is False
    assert after["migration"] == MIGRATION_VERSION

    with psycopg.connect(postgres_database) as connection:
        row = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = %s",
            (MIGRATION_VERSION,),
        ).fetchone()

    assert row is not None
    assert row[0] == MIGRATION_VERSION
