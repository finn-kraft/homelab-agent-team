from __future__ import annotations

import json
import re
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
            # Keep the standalone EngineeringAgent CLI compatible with the
            # same durable session tables used by agent-orchestrator.
            connection.execute("""
            CREATE TABLE IF NOT EXISTS engineering_sessions (
              id BIGSERIAL PRIMARY KEY,
              job_id BIGINT NOT NULL REFERENCES jobs(id),
              step_id BIGINT REFERENCES steps(id),
              worker_id TEXT NOT NULL,
              starting_commit TEXT,
              current_model_tier TEXT NOT NULL DEFAULT 'local',
              current_problem TEXT,
              progress_score DOUBLE PRECISION NOT NULL DEFAULT 0,
              stagnation_count INTEGER NOT NULL DEFAULT 0,
              turn_count INTEGER NOT NULL DEFAULT 0,
              last_successful_action TEXT,
              last_test_result JSONB,
              lease_expires_at TIMESTAMPTZ,
              started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              completed_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS engineering_actions (
              id BIGSERIAL PRIMARY KEY,
              session_id BIGINT NOT NULL REFERENCES engineering_sessions(id),
              sequence INTEGER NOT NULL,
              model TEXT,
              action TEXT NOT NULL,
              observation TEXT,
              progress_classification TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE(session_id, sequence)
            );
            """)

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
                """UPDATE steps s SET lease_expires_at=now()+(%s*interval '1 second'),
                updated_at=now() FROM jobs j WHERE s.id=%s AND s.worker_id=%s
                AND s.status='running' AND j.id=s.job_id AND j.status='running'""",
                (lease_seconds, step_id, worker_id),
            )
            return cursor.rowcount == 1

    def job_status(self, job_id: int) -> str | None:
        """Read the workflow control state for cooperative pause/cancel."""
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return row[0] if row else None

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

    def record_command(self, step_id: int, result: CommandResult, attempt: int | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO command_runs
                (step_id, argv, stdout, stderr, exit_code, duration_seconds, timed_out, source, attempt)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'coder-agent',%s)""",
                (step_id, json.dumps(result.argv), self._redact_command_output(result.stdout),
                 self._redact_command_output(result.stderr),
                 result.exit_code, result.duration_seconds, result.timed_out, attempt),
            )

    @staticmethod
    def _redact_command_output(value: str) -> str:
        """Keep audit evidence useful without persisting credentials verbatim."""
        patterns = (
            r"(?i)(?:api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,]+",
            r"\bpostgres(?:ql)?(?:\+[A-Za-z0-9_-]+)?://[^\s]+",
            r"\bsk-[A-Za-z0-9_-]{16,}\b",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        )
        for pattern in patterns:
            value = re.sub(pattern, "[REDACTED]", value)
        return value[:50_000]

    def event(self, task: Task, kind: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO events(job_id,step_id,agent,event_type,structured_payload)
                VALUES(%s,%s,'coder-agent',%s,%s)""",
                (task.job_id, task.step_id, kind, json.dumps(payload)),
            )

    def start_engineering_session(self, job_id, step_id, worker_id, starting_commit=None):
        with self.connect() as connection:
            row = connection.execute("""INSERT INTO engineering_sessions
                (job_id,step_id,worker_id,starting_commit) VALUES(%s,%s,%s,%s) RETURNING id""",
                (job_id, step_id, worker_id, starting_commit)).fetchone()
            # ``coder_agent.Store.connect`` intentionally uses psycopg's
            # default tuple row factory (the legacy claim path relies on
            # positional access).  Keep the V2 session helpers consistent
            # with that contract; indexing a tuple by ``"id"`` crashes the
            # orchestrator before the model gets its first turn.
            return row[0]

    def resume_engineering_session(self, job_id, step_id=None):
        with self.connect() as connection:
            row = connection.execute("""SELECT id FROM engineering_sessions WHERE job_id=%s
                AND (%s IS NULL OR step_id=%s) AND completed_at IS NULL
                ORDER BY updated_at DESC LIMIT 1""", (job_id, step_id, step_id)).fetchone()
            return row[0] if row else None

    def engineering_baseline(self, job_id, step_id=None):
        """Recover the original repository baseline for a resumed session."""
        with self.connect() as connection:
            session = connection.execute("""SELECT starting_commit FROM engineering_sessions
                WHERE job_id=%s AND (%s IS NULL OR step_id=%s) AND completed_at IS NULL
                ORDER BY updated_at DESC LIMIT 1""", (job_id, step_id, step_id)).fetchone()
            if not session:
                return {}
            event = connection.execute("""SELECT structured_payload FROM events
                WHERE job_id=%s AND step_id IS NOT DISTINCT FROM %s
                  AND agent IN ('coder-agent','engineering-agent')
                  AND event_type='started' ORDER BY id ASC LIMIT 1""",
                (job_id, step_id)).fetchone()
            payload = event[0] if event else {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            return {
                "starting_commit": session[0],
                "preexisting_changes": list(payload.get("preexisting_changes") or [])
                if isinstance(payload, dict) else [],
            }

    def record_engineering_action(self, session_id, sequence, action, observation="",
                                  model=None, progress_classification=None):
        with self.connect() as connection:
            connection.execute("""INSERT INTO engineering_actions
                (session_id,sequence,model,action,observation,progress_classification)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id,sequence) DO UPDATE SET
                model=EXCLUDED.model, action=EXCLUDED.action, observation=EXCLUDED.observation,
                progress_classification=EXCLUDED.progress_classification""",
                (session_id, sequence, model, action, observation[:30000], progress_classification))
            connection.execute("UPDATE engineering_sessions SET turn_count=%s,updated_at=now() WHERE id=%s",
                               (sequence, session_id))

    def complete_engineering_session(self, session_id):
        with self.connect() as connection:
            connection.execute("UPDATE engineering_sessions SET completed_at=now(),updated_at=now() WHERE id=%s",
                               (session_id,))


# Durable persistence remains shared with V1 while the public role moves to
# EngineeringAgent.
EngineeringStore = Store
