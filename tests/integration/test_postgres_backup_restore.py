from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from psycopg import sql

from agent_core.postgres_backup import BackupError, PostgresBackupManager


def _service_file(path: Path, database_url: str, target_database: str) -> Path:
    parsed = urlsplit(database_url)
    query = parse_qs(parsed.query)
    host = query.get("host", [parsed.hostname or "127.0.0.1"])[0]
    port = query.get("port", [str(parsed.port or 5432)])[0]
    user = parsed.username or os.getenv("USER", "postgres")
    source_database = parsed.path.lstrip("/")
    path.write_text(
        f"""[agent_source]
host={host}
port={port}
user={user}
dbname={source_database}

[agent_admin]
host={host}
port={port}
user={user}
dbname=postgres

[agent_target]
host={host}
port={port}
user={user}
dbname={target_database}
"""
    )
    path.chmod(0o600)
    return path


def test_backup_is_checksummed_restored_into_scratch_and_recoverable(
    postgres_database, tmp_path, monkeypatch
):
    with psycopg.connect(postgres_database) as connection:
        connection.execute("CREATE TABLE resilience_probe(value text NOT NULL)")
        connection.execute("INSERT INTO resilience_probe VALUES ('durable')")
        connection.commit()

    admin_url = os.environ["TEST_DATABASE_ADMIN_URL"]
    target_database = f"agent_restore_target_{uuid.uuid4().hex[:10]}"
    service_file = _service_file(tmp_path / "pg_service.conf", postgres_database, target_database)
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    manager = PostgresBackupManager()

    backup = manager.create(tmp_path / "backups", service="agent_source")
    metadata = Path(f"{backup}.json")
    assert backup.exists() and backup.stat().st_mode & 0o077 == 0
    assert metadata.exists() and metadata.stat().st_mode & 0o077 == 0

    verification = manager.verify(backup, admin_service="agent_admin")
    assert verification["verified"] is True
    assert verification["table_count"] >= 1

    parsed = urlsplit(admin_url)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_database)))
    try:
        restored = manager.restore(
            backup, target_service="agent_target", confirm_empty_target=True
        )
        assert restored["restored"] is True
        target_url = parsed._replace(path=f"/{target_database}").geturl()
        with psycopg.connect(target_url) as connection:
            value = connection.execute("SELECT value FROM resilience_probe").fetchone()[0]
        assert value == "durable"
        with pytest.raises(BackupError, match="not empty"):
            manager.restore(
                backup, target_service="agent_target", confirm_empty_target=True
            )

        with backup.open("ab") as stream:
            stream.write(b"tampered")
        with pytest.raises(BackupError, match="SHA-256"):
            manager.verify(backup, admin_service="agent_admin")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()",
                (target_database,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_database))
            )
