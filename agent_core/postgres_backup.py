from __future__ import annotations

"""Conservative PostgreSQL backup, verification, and restore primitives.

Connections use named libpq service profiles.  Passwords therefore stay in an
operator-owned ``.pgpass`` or secret-mounted service configuration and never
appear in command arguments, metadata, or log messages.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .redaction import redact_text


class BackupError(RuntimeError):
    """A backup operation failed without exposing subprocess credentials."""


_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_ENVIRONMENT = {"PATH", "LANG", "LC_ALL", "TZ"}


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PostgresBackupManager:
    """Create custom-format backups and prove them with a scratch restore."""

    def __init__(self, *, environment: Mapping[str, str] | None = None):
        source = dict(os.environ if environment is None else environment)
        self.environment = {
            key: value
            for key, value in source.items()
            if key in _SAFE_ENVIRONMENT or key.startswith("PG")
        }
        self.tools = {
            name: shutil.which(name, path=self.environment.get("PATH"))
            for name in ("pg_dump", "pg_restore", "createdb", "dropdb", "psql")
        }
        missing = sorted(name for name, path in self.tools.items() if not path)
        if missing:
            raise BackupError(f"required PostgreSQL client tools are missing: {', '.join(missing)}")

    @staticmethod
    def _service(value: str) -> str:
        if not value or not _SERVICE_NAME.fullmatch(value):
            raise ValueError("libpq service names may contain only letters, numbers, . _ and -")
        return value

    def _env(self, service: str) -> dict[str, str]:
        result = dict(self.environment)
        result["PGSERVICE"] = self._service(service)
        return result

    def _run(
        self,
        tool: str,
        *args: str,
        service: str | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(self.environment)
        if service:
            environment = self._env(service)
        try:
            result = subprocess.run(
                [str(self.tools[tool]), *args],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackupError(f"{tool} exceeded its {timeout}-second timeout") from exc
        if result.returncode:
            detail = redact_text(result.stderr or result.stdout or "no diagnostic output", limit=4_000)
            raise BackupError(f"{tool} failed with exit code {result.returncode}: {detail}")
        return result

    @staticmethod
    def metadata_path(backup: str | Path) -> Path:
        return Path(f"{Path(backup)}.json")

    def _validate_archive(self, backup: str | Path) -> tuple[Path, dict[str, Any]]:
        path = Path(backup).expanduser().resolve(strict=True)
        metadata_path = self.metadata_path(path)
        if not metadata_path.is_file():
            raise BackupError("backup metadata is missing; refusing an unverifiable archive")
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError("backup metadata is unreadable") from exc
        try:
            expected_size = int(metadata.get("size_bytes", -1))
        except (TypeError, ValueError) as exc:
            raise BackupError("backup metadata has an invalid size") from exc
        expected_checksum = str(metadata.get("sha256", ""))
        if path.stat().st_size != expected_size or _checksum(path) != expected_checksum:
            raise BackupError("backup size or SHA-256 checksum does not match its metadata")
        self._run("pg_restore", "--list", str(path), timeout=300)
        return path, metadata

    def create(self, output_directory: str | Path, *, service: str) -> Path:
        """Create an atomic, mode-0600 custom-format archive plus checksum."""
        service = self._service(service)
        directory = Path(output_directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(directory.stat().st_mode & ~0o077)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"agent-team-{stamp}-{uuid.uuid4().hex[:8]}.dump"
        final_path = directory / name
        temporary = directory / f".{name}.partial"
        metadata_path = self.metadata_path(final_path)
        temporary_metadata = directory / f".{name}.json.partial"
        try:
            temporary.touch(mode=0o600, exist_ok=False)
            self._run(
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={temporary}",
                service=service,
            )
            if temporary.stat().st_size <= 0:
                raise BackupError("pg_dump produced an empty archive")
            metadata = {
                "format_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "archive_format": "postgresql-custom",
                "sha256": _checksum(temporary),
                "size_bytes": temporary.stat().st_size,
                "credential_source": "external-libpq-service",
                "pg_dump_version": self._run("pg_dump", "--version", timeout=30).stdout.strip(),
            }
            temporary_metadata.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
            temporary_metadata.chmod(0o600)
            os.replace(temporary, final_path)
            final_path.chmod(0o600)
            os.replace(temporary_metadata, metadata_path)
            metadata_path.chmod(0o600)
            return final_path
        finally:
            for unfinished in (temporary, temporary_metadata):
                if unfinished.exists():
                    unfinished.unlink()

    def verify(self, backup: str | Path, *, admin_service: str) -> dict[str, Any]:
        """Verify checksum/archive structure and perform a disposable restore."""
        admin_service = self._service(admin_service)
        path, metadata = self._validate_archive(backup)
        scratch = f"agent_backup_verify_{uuid.uuid4().hex[:12]}"
        created = False
        primary_error: Exception | None = None
        try:
            self._run("createdb", scratch, service=admin_service, timeout=120)
            created = True
            connection = f"service={admin_service} dbname={scratch}"
            self._run(
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                f"--dbname={connection}",
                str(path),
            )
            result = self._run(
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                f"--dbname={connection}",
                "--command=SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public';",
                timeout=120,
            )
            try:
                table_count = int(result.stdout.strip())
            except ValueError as exc:
                raise BackupError("scratch restore returned an invalid table count") from exc
            if table_count <= 0:
                raise BackupError("scratch restore contains no public tables")
            return {
                "verified": True,
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size_bytes"],
                "table_count": table_count,
            }
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            if created:
                try:
                    self._run(
                        "dropdb", "--if-exists", scratch,
                        service=admin_service, timeout=120,
                    )
                except Exception:
                    if primary_error is None:
                        raise

    def restore(
        self,
        backup: str | Path,
        *,
        target_service: str,
        confirm_empty_target: bool = False,
    ) -> dict[str, Any]:
        """Restore into an operator-created target only after proving it is empty."""
        if not confirm_empty_target:
            raise ValueError("restore requires explicit confirmation of an empty target")
        target_service = self._service(target_service)
        path, metadata = self._validate_archive(backup)
        connection = f"service={target_service}"
        result = self._run(
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            f"--dbname={connection}",
            "--command=SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public';",
            service=target_service,
            timeout=120,
        )
        try:
            existing_tables = int(result.stdout.strip())
        except ValueError as exc:
            raise BackupError("target database returned an invalid table count") from exc
        if existing_tables:
            raise BackupError("target database is not empty; refusing a destructive restore")
        self._run(
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            f"--dbname={connection}",
            str(path),
            service=target_service,
        )
        return {
            "restored": True,
            "sha256": metadata["sha256"],
            "size_bytes": metadata["size_bytes"],
        }
