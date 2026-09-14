from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import CommandResult, Status, Task


class Store:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install coder-agent to enable PostgreSQL support") from exc
        with psycopg.connect(self.database_url) as connection:
            yield connection

    def migrate(self) -> None:
        sql = Path(__file__).parent.parent.joinpath("agent_core", "schema.sql").read_text()
        with self.connect() as connection:
            connection.execute(sql)

    def claim(self, worker_id: str, lease_seconds: int = 300) -> Task | None:
        """Atomically claims one queued/retryable step without double assignment."""
        with self.connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT s.id FROM steps s JOIN jobs j ON j.id = s.job_id
                  WHERE j.status = 'running'
                    AND s.status IN ('queued', 'changes_requested')
                    AND (s.lease_expires_at IS NULL OR s.lease_expires_at < now())
                  ORDER BY j.priority DESC, s.id
                  FOR UPDATE OF s SKIP LOCKED LIMIT 1
                )
                UPDATE steps s SET status='running', worker_id=%s,
                  lease_expires_at=now() + (%s * interval '1 second'),
                  attempt_count=attempt_count + 1, updated_at=now(),
                  started_at=COALESCE(started_at,now())
                FROM candidate WHERE s.id=candidate.id
                RETURNING s.job_id, s.id, s.repository, s.branch, s.objective,
                  s.acceptance_criteria, s.constraints, s.status, s.attempt_count,
                  s.reviewer_feedback
                """, (worker_id, lease_seconds)
            ).fetchone()
            if not row:
                return None
            return Task(row[0], row[1], row[2], row[3], row[4], list(row[5]),
                        list(row[6]), Status(row[7]), row[8], row[9])

    def heartbeat(self, step_id: int, worker_id: str, lease_seconds: int = 300) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE steps SET lease_expires_at=now()+(%s*interval '1 second'),
                updated_at=now() WHERE id=%s AND worker_id=%s AND status='running'""",
                (lease_seconds, step_id, worker_id),
            )
            return cursor.rowcount == 1

    def update_step(self, step_id: int, worker_id: str, status: Status,
                    **fields: Any) -> None:
        allowed = {"starting_commit", "resulting_commit", "files_changed", "model_used",
                   "coder_response", "blocker"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"invalid step fields: {sorted(invalid)}")
        assignments = ["status=%s", "updated_at=now()", "lease_expires_at=NULL"]
        values: list[Any] = [status.value]
        for key, value in fields.items():
            assignments.append(f"{key}=%s")
            values.append(json.dumps(value) if isinstance(value, (dict, list)) else value)
        values.extend([step_id, worker_id])
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE steps SET {', '.join(assignments)} WHERE id=%s AND worker_id=%s",
                values,
            )
            if cursor.rowcount != 1:
                raise RuntimeError("step lease is no longer owned by this worker")

    def record_command(self, step_id: int, result: CommandResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO command_runs
                (step_id, argv, stdout, stderr, exit_code, duration_seconds, timed_out)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (step_id, json.dumps(result.argv), result.stdout, result.stderr,
                 result.exit_code, result.duration_seconds, result.timed_out),
            )

    def event(self, task: Task, kind: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO events(job_id,step_id,agent,event_type,structured_payload)
                VALUES(%s,%s,'coder-agent',%s,%s)""",
                (task.job_id, task.step_id, kind, json.dumps(payload)),
            )
