from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from .postgres_backup import BackupError, PostgresBackupManager
from .structured_logging import configure_logging, log_event


LOG = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-db-backup",
        description="Credential-safe PostgreSQL backup, scratch verification, and restore.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create an atomic custom-format backup")
    create.add_argument("output_directory", type=Path)
    create.add_argument("--service", default=os.getenv("PGSERVICE"))
    create.add_argument("--verify-admin-service")
    verify = commands.add_parser("verify", help="checksum and scratch-restore a backup")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--admin-service", required=True)
    restore = commands.add_parser("restore", help="restore into an existing empty database")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target-service", required=True)
    restore.add_argument("--confirm-empty-target", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("agent-db-backup")
    try:
        manager = PostgresBackupManager()
        if args.command == "create":
            if not args.service:
                raise ValueError("--service or PGSERVICE is required")
            backup = manager.create(args.output_directory, service=args.service)
            result: dict[str, object] = {"created": True, "backup": str(backup)}
            if args.verify_admin_service:
                result["verification"] = manager.verify(
                    backup, admin_service=args.verify_admin_service
                )
        elif args.command == "verify":
            result = manager.verify(args.backup, admin_service=args.admin_service)
        else:
            result = manager.restore(
                args.backup,
                target_service=args.target_service,
                confirm_empty_target=args.confirm_empty_target,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OSError, ValueError) as exc:
        log_event(LOG, logging.ERROR, "database_backup_operation_failed", error=str(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
