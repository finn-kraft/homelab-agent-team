from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql


def _database_url(admin_url: str, database: str) -> str:
    parts = urlsplit(admin_url)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"/{database}",
            parts.query,
            parts.fragment,
        )
    )


@pytest.fixture
def postgres_database():
    """Create a real disposable PostgreSQL database for one integration test."""
    admin_url = os.getenv("TEST_DATABASE_ADMIN_URL")

    if not admin_url:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not configured")

    database = f"agent_team_test_{uuid.uuid4().hex[:12]}"
    test_url = _database_url(admin_url, database)

    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(database)
            )
        )

    try:
        yield test_url

    finally:
        # Terminate anything that accidentally retained a test connection.
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (database,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database)
                )
            )
