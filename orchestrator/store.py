from __future__ import annotations

"""PostgreSQL persistence for deterministic orchestration state.

This module intentionally extends the existing agent-team tables instead of
introducing a second workflow database.  All state changes are small,
transactional, and accompanied by an append-only event.
"""

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from coder_agent.models import Status, Task


MIGRATION_VERSION = "orchestrator-0003"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE steps ADD COLUMN IF NOT EXISTS orchestrator_worker_id TEXT;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS orchestrator_lease_expires_at TIMESTAMPTZ;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS verification_result JSONB;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS checkpoint_marker TEXT;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS checkpoint_started_at TIMESTAMPTZ;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS approved_files JSONB NOT NULL DEFAULT '[]';
ALTER TABLE command_runs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'coder-agent';
ALTER TABLE command_runs ADD COLUMN IF NOT EXISTS attempt INTEGER;

CREATE TABLE IF NOT EXISTS repository_locks (
  repository TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verification_runs (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES jobs(id),
  step_id BIGINT NOT NULL REFERENCES steps(id),
  attempt INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL,
  commands JSONB NOT NULL DEFAULT '[]',
  details JSONB NOT NULL DEFAULT '{}',
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(step_id, attempt)
);

CREATE TABLE IF NOT EXISTS checkpoint_runs (
  step_id BIGINT PRIMARY KEY REFERENCES steps(id),
  job_id BIGINT NOT NULL REFERENCES jobs(id),
  marker TEXT NOT NULL UNIQUE,
  worker_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  approved_files JSONB NOT NULL DEFAULT '[]',
  starting_commit TEXT,
  commit_sha TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS llm_invocations (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT REFERENCES jobs(id),
  step_id BIGINT REFERENCES steps(id),
  caller_agent TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  route_reason TEXT,
  attempt INTEGER,
  latency_seconds DOUBLE PRECISION,
  usage JSONB,
  estimated_cloud_cost NUMERIC(14,6),
  fallback BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  component TEXT NOT NULL,
  status TEXT NOT NULL,
  current_job_id BIGINT REFERENCES jobs(id),
  current_step_id BIGINT REFERENCES steps(id),
  current_action TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS control_center_credentials (
  id SMALLINT PRIMARY KEY CHECK (id = 1),
  password_hash TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS missions (
  id BIGSERIAL PRIMARY KEY,
  goal TEXT NOT NULL,
  repository TEXT NOT NULL,
  branch TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  cloud_budget NUMERIC(14,6),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work_packages (
  id BIGSERIAL PRIMARY KEY,
  mission_id BIGINT NOT NULL REFERENCES missions(id),
  objective TEXT NOT NULL,
  acceptance_criteria JSONB NOT NULL DEFAULT '[]',
  constraints JSONB NOT NULL DEFAULT '[]',
  roadmap_reference TEXT,
  dependencies JSONB NOT NULL DEFAULT '[]',
  repository TEXT NOT NULL,
  branch TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready',
  starting_commit TEXT,
  worktree TEXT,
  worker_id TEXT,
  lease_expires_at TIMESTAMPTZ,
  resulting_commit TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_packages_claim_idx ON work_packages(status, mission_id, id);

CREATE INDEX IF NOT EXISTS steps_orchestrator_claim_idx
  ON steps (status, orchestrator_lease_expires_at, id);
CREATE INDEX IF NOT EXISTS verification_runs_step_idx
  ON verification_runs (step_id, attempt DESC);
CREATE INDEX IF NOT EXISTS llm_invocations_job_idx
  ON llm_invocations (job_id, step_id, created_at DESC);
CREATE INDEX IF NOT EXISTS command_runs_step_source_attempt_idx
  ON command_runs (step_id, source, attempt, id);
"""


class OrchestratorStore:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - installation error
            raise RuntimeError("install homelab-agent-team with PostgreSQL support") from exc
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def migrate(self) -> None:
        """Apply the base schema and additive orchestration migration safely."""
        base_schema = Path(__file__).parent.parent / "agent_core" / "schema.sql"
        with self.connect() as connection:
            connection.execute(base_schema.read_text())
            connection.execute(MIGRATION_SQL)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES(%s) ON CONFLICT(version) DO NOTHING",
                (MIGRATION_VERSION,),
            )

    # ------------------------------------------------------------------
    # Durable events and read models
    # ------------------------------------------------------------------
    @staticmethod
    def _event(
        connection,
        job_id: int,
        step_id: int | None,
        event_type: str,
        payload: dict[str, Any],
        *,
        agent: str = "agent-orchestrator",
    ) -> None:
        connection.execute(
            """INSERT INTO events(job_id,step_id,agent,event_type,structured_payload)
            VALUES(%s,%s,%s,%s,%s)""",
            (job_id, step_id, agent, event_type, json.dumps(payload, default=str)),
        )

    @staticmethod
    def _canonical_repository(repository: str) -> str:
        return os.path.realpath(repository)

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return []
            return OrchestratorStore._json_list(parsed)
        return []

    def job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return dict(row) if row else None

    def step(self, step_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM steps WHERE id=%s", (step_id,)).fetchone()
            return dict(row) if row else None

    def preexisting_files(self, step_id: int) -> list[str]:
        """Return the Coder's immutable baseline for a step's current attempt."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT structured_payload FROM events
                WHERE step_id=%s AND agent='coder-agent' AND event_type='started'
                ORDER BY id DESC LIMIT 1""", (step_id,)
            ).fetchone()
            if not row:
                return []
            payload = row["structured_payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    return []
            return self._json_list(payload.get("preexisting_changes")) if isinstance(payload, dict) else []

    def inspect(self, job_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if not job:
                raise KeyError(f"job {job_id} does not exist")
            steps = list(connection.execute(
                "SELECT * FROM steps WHERE job_id=%s ORDER BY sequence", (job_id,)
            ).fetchall())
            events = list(connection.execute(
                """SELECT id,step_id,agent,event_type,structured_payload,created_at
                FROM events WHERE job_id=%s ORDER BY id DESC LIMIT 100""",
                (job_id,),
            ).fetchall())
            return {"job": dict(job), "steps": [dict(row) for row in steps],
                    "events": [dict(row) for row in events]}

    def status(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT j.id,j.goal,j.repository,j.branch,j.status,j.current_phase,
                j.current_step,j.iteration_count,j.max_iterations,j.updated_at,
                j.planner_worker_id,j.planner_lease_expires_at,
                COUNT(s.id) FILTER (WHERE s.status <> 'complete') AS open_steps,
                COUNT(s.id) FILTER (WHERE s.status = 'complete') AS completed_steps,
                (SELECT e.structured_payload->>'progress_classification' FROM events e
                 JOIN steps es ON es.id=e.step_id WHERE es.job_id=j.id
                 AND e.event_type='agent_action' ORDER BY e.id DESC LIMIT 1) AS progress_classification,
                (SELECT s2.blocker FROM steps s2 WHERE s2.job_id=j.id
                 AND s2.status IN ('blocked','failed','needs_human')
                 ORDER BY s2.updated_at DESC NULLS LAST, s2.id DESC LIMIT 1) AS blocker
                FROM jobs j LEFT JOIN steps s ON s.job_id=j.id
                GROUP BY j.id ORDER BY j.priority DESC,j.id"""
            ).fetchall()
            return [dict(row) for row in rows]

    def invariant_report(self) -> list[dict[str, Any]]:
        """Return read-only workflow invariant violations for operators."""
        checks = {
            "planning_without_lease": "SELECT id FROM jobs WHERE status='planning' AND planner_worker_id IS NULL",
            "multiple_active_steps": "SELECT job_id FROM steps WHERE status IN ('queued','running','review','changes_requested','verification') GROUP BY job_id HAVING count(*) > 1",
            "completed_without_commit": "SELECT id FROM steps WHERE status='complete' AND (resulting_commit IS NULL OR resulting_commit='')",
            "checkpoint_without_verification": "SELECT id FROM steps WHERE status='checkpoint' AND verification_result IS NULL",
        }
        violations = []
        with self.connect() as connection:
            for name, query in checks.items():
                rows = connection.execute(query).fetchall()
                if rows:
                    violations.append({"invariant": name, "count": len(rows),
                                       "ids": [row[0] for row in rows[:50]]})
        return violations

    def create_mission(self, goal: str, repository: str, branch: str,
                       cloud_budget: float | None = None) -> int:
        with self.connect() as connection:
            row = connection.execute("""INSERT INTO missions(goal,repository,branch,cloud_budget)
                VALUES(%s,%s,%s,%s) RETURNING id""", (goal, repository, branch, cloud_budget)).fetchone()
            return row["id"]

    def create_work_package(self, mission_id: int, objective: str, repository: str,
                            branch: str, acceptance_criteria: list[str],
                            constraints: list[str] | None = None, roadmap_reference: str | None = None,
                            dependencies: list[int] | None = None) -> int:
        with self.connect() as connection:
            row = connection.execute("""INSERT INTO work_packages
                (mission_id,objective,acceptance_criteria,constraints,roadmap_reference,dependencies,repository,branch)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (mission_id, objective, json.dumps(acceptance_criteria), json.dumps(constraints or []),
                 roadmap_reference, json.dumps(dependencies or []), repository, branch)).fetchone()
            return row["id"]

    def list_work_packages(self, mission_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            query = "SELECT * FROM work_packages"
            params: tuple[Any, ...] = ()
            if mission_id is not None:
                query += " WHERE mission_id=%s"; params = (mission_id,)
            query += " ORDER BY id"
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def claim_work_package(self, worker_id: str, lease_seconds: int = 900,
                           worktree_manager=None) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("""WITH candidate AS (
                SELECT p.id FROM work_packages p JOIN missions m ON m.id=p.mission_id
                WHERE p.status='ready' AND m.status='active'
                  AND (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
                  AND NOT EXISTS (SELECT 1 FROM work_packages d
                    WHERE d.id = ANY(SELECT jsonb_array_elements_text(p.dependencies)::bigint)
                    AND d.status <> 'complete')
                ORDER BY p.id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE work_packages p SET status='engineering',worker_id=%s,
                  lease_expires_at=now()+(%s*interval '1 second'),updated_at=now()
                FROM candidate WHERE p.id=candidate.id RETURNING p.*""", (worker_id, lease_seconds)).fetchone()
            if not row:
                return None
            package = dict(row)
            if worktree_manager is not None and not package.get("worktree"):
                worktree = worktree_manager.create(package["repository"], package["mission_id"],
                                                   package["id"], package["branch"])
                connection.execute("UPDATE work_packages SET worktree=%s,starting_commit=%s,branch=%s WHERE id=%s",
                                   (str(worktree.path), worktree.starting_commit, worktree.branch, package["id"]))
                package.update(worktree=str(worktree.path), starting_commit=worktree.starting_commit,
                               branch=worktree.branch)
            return package

    def resume_engineering_session(self, job_id: int, step_id: int | None = None) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("""SELECT * FROM engineering_sessions WHERE job_id=%s
                AND (%s IS NULL OR step_id=%s) AND completed_at IS NULL
                ORDER BY updated_at DESC LIMIT 1""", (job_id, step_id, step_id)).fetchone()
            return dict(row) if row else None

    def update_work_package(self, package_id: int, worker_id: str, status: str,
                            resulting_commit: str | None = None) -> None:
        if status not in {'engineering', 'review', 'verifying', 'complete', 'blocked'}:
            raise ValueError('invalid work package status')
        with self.connect() as connection:
            result = connection.execute("""UPDATE work_packages SET status=%s,
                resulting_commit=COALESCE(%s,resulting_commit),lease_expires_at=NULL,
                updated_at=now() WHERE id=%s AND worker_id=%s""",
                (status, resulting_commit, package_id, worker_id))
            if result.rowcount != 1:
                raise RuntimeError('work package lease is no longer owned')

    def heartbeat_work_package(self, package_id: int, worker_id: str,
                               lease_seconds: int = 900) -> bool:
        with self.connect() as connection:
            result = connection.execute("""UPDATE work_packages SET lease_expires_at=
                now()+(%s*interval '1 second'), updated_at=now()
                WHERE id=%s AND worker_id=%s AND status IN ('engineering','review','verifying')""",
                (lease_seconds, package_id, worker_id))
            return result.rowcount == 1

    def recover_work_packages(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute("""UPDATE work_packages SET status='ready',worker_id=NULL,
                lease_expires_at=NULL,updated_at=now() WHERE status IN ('engineering','review','verifying')
                AND lease_expires_at < now() RETURNING id""").fetchall()
            return [row["id"] for row in rows]

    def start_engineering_session(self, job_id: int, step_id: int | None,
                                  worker_id: str, starting_commit: str | None = None) -> int:
        with self.connect() as connection:
            row = connection.execute("""INSERT INTO engineering_sessions
                (job_id,step_id,worker_id,starting_commit) VALUES(%s,%s,%s,%s)
                RETURNING id""", (job_id, step_id, worker_id, starting_commit)).fetchone()
            return row["id"]

    def record_engineering_action(self, session_id: int, sequence: int, action: str,
                                  observation: str = "", model: str | None = None,
                                  progress_classification: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute("""INSERT INTO engineering_actions
                (session_id,sequence,model,action,observation,progress_classification)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id,sequence) DO UPDATE SET
                model=EXCLUDED.model,action=EXCLUDED.action,observation=EXCLUDED.observation,
                progress_classification=EXCLUDED.progress_classification""",
                (session_id, sequence, model, action, observation[:30000], progress_classification))
            connection.execute("""UPDATE engineering_sessions SET turn_count=%s,
                last_successful_action=CASE WHEN %s NOT IN ('invalid','no_progress') THEN %s ELSE last_successful_action END,
                updated_at=now() WHERE id=%s""", (sequence, action, action, session_id))

    # ------------------------------------------------------------------
    # Repository mutation lease
    # ------------------------------------------------------------------
    def _acquire_repository_lock(self, connection, repository: str, owner: str,
                                 lease_seconds: int) -> bool:
        row = connection.execute(
            """INSERT INTO repository_locks(repository,owner,lease_expires_at,heartbeat_at,updated_at)
            VALUES(%s,%s,now()+(%s*interval '1 second'),now(),now())
            ON CONFLICT(repository) DO UPDATE SET
              owner=EXCLUDED.owner,
              lease_expires_at=EXCLUDED.lease_expires_at,
              heartbeat_at=now(),updated_at=now()
            WHERE repository_locks.owner=EXCLUDED.owner
               OR repository_locks.lease_expires_at < now()
            RETURNING repository""",
            (self._canonical_repository(repository), owner, lease_seconds),
        ).fetchone()
        return row is not None

    def acquire_repository_lock(self, repository: str, owner: str, lease_seconds: int) -> bool:
        with self.connect() as connection:
            return self._acquire_repository_lock(connection, repository, owner, lease_seconds)

    def heartbeat_repository_lock(self, repository: str, owner: str,
                                  lease_seconds: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE repository_locks SET lease_expires_at=now()+(%s*interval '1 second'),
                heartbeat_at=now(),updated_at=now() WHERE repository=%s AND owner=%s""",
                (lease_seconds, self._canonical_repository(repository), owner),
            )
            return result.rowcount == 1

    def release_repository_lock(self, repository: str, owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM repository_locks WHERE repository=%s AND owner=%s",
                (self._canonical_repository(repository), owner),
            )

    def heartbeat_coding(self, step_id: int, worker_id: str, lease_seconds: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE steps SET lease_expires_at=now()+(%s*interval '1 second'),updated_at=now()
                WHERE id=%s AND worker_id=%s AND status='running'""",
                (lease_seconds, step_id, worker_id),
            )
            return result.rowcount == 1

    def heartbeat_orchestration(self, step_id: int, worker_id: str,
                                lease_seconds: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE steps SET orchestrator_lease_expires_at=now()+(%s*interval '1 second'),
                updated_at=now() WHERE id=%s AND orchestrator_worker_id=%s
                AND status IN ('verification','checkpoint')""",
                (lease_seconds, step_id, worker_id),
            )
            return result.rowcount == 1

    # ------------------------------------------------------------------
    # Coder and Reviewer hand-offs
    # ------------------------------------------------------------------
    def claim_coding(self, worker_id: str, lease_seconds: int,
                     repository_lock_seconds: int) -> Task | None:
        """Claim exactly one mutation step and its repository lease atomically."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.* FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE j.status='running' AND s.status IN ('queued','changes_requested')
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at < now())
                ORDER BY j.priority DESC,s.id FOR UPDATE OF s,j SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            owner = f"{worker_id}:coder:{row['id']}"
            if not self._acquire_repository_lock(
                connection, row["repository"], owner, repository_lock_seconds
            ):
                return None
            connection.execute(
                """UPDATE steps SET status='running',worker_id=%s,
                lease_expires_at=now()+(%s*interval '1 second'),
                attempt_count=attempt_count+1,updated_at=now(),
                started_at=COALESCE(started_at,now()) WHERE id=%s""",
                (worker_id, lease_seconds, row["id"]),
            )
            attempt = int(row["attempt_count"]) + 1
            self._event(connection, row["job_id"], row["id"], "coding_started", {
                "worker_id": worker_id, "attempt": attempt,
                "repository_lock_owner": owner,
            })
            return Task(
                row["job_id"], row["id"], row["repository"], row["branch"],
                row["objective"], self._json_list(row["acceptance_criteria"]),
                self._json_list(row["constraints"]), Status.RUNNING, attempt,
                row.get("reviewer_feedback"),
            )

    def finish_coding_handoff(self, step_id: int, worker_id: str) -> str | None:
        """Synchronize a bounded Coder call with job-level workflow state."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.id=%s FOR UPDATE OF s,j", (step_id,)
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            status = row["status"]
            controlled = row["job_status"] in {"paused", "cancelled"}
            if status == "review":
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='reviewing',current_phase='review',updated_at=now() WHERE id=%s",
                        (row["job_id"],),
                    )
                self._event(connection, row["job_id"], step_id, "coding_finished", {
                    "worker_id": worker_id, "attempt": row["attempt_count"],
                    "files_changed": self._json_list(row["files_changed"]),
                })
            elif status in {"blocked", "needs_human"}:
                job_status = "needs_human" if status == "needs_human" else "blocked"
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status=%s,current_phase=%s,updated_at=now() WHERE id=%s",
                        (job_status, status, row["job_id"]),
                    )
                self._event(connection, row["job_id"], step_id, f"coding_{status}", {
                    "worker_id": worker_id, "blocker": row.get("blocker"),
                })
            elif status == "failed":
                # Failed attempts remain visible to the Planner; it can retry/re-scope.
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='running',current_phase='planning',updated_at=now() WHERE id=%s",
                        (row["job_id"],),
                    )
                self._event(connection, row["job_id"], step_id, "coding_failed", {
                    "worker_id": worker_id, "blocker": row.get("blocker"),
                })
            return status

    def next_review_step(self) -> int | None:
        """Mark one ready step as reviewing, then let Reviewer claim that exact ID."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.id,s.job_id FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE s.status='review' AND j.status IN ('running','reviewing')
                  AND (s.review_lease_expires_at IS NULL OR s.review_lease_expires_at < now())
                ORDER BY j.priority DESC,s.id FOR UPDATE OF s,j SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE jobs SET status='reviewing',current_phase='review',current_step=%s,updated_at=now() WHERE id=%s",
                (row["id"], row["job_id"]),
            )
            self._event(connection, row["job_id"], row["id"], "review_requested", {})
            return int(row["id"])

    def finish_review_handoff(self, step_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.id=%s FOR UPDATE OF s,j", (step_id,)
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            status = row["status"]
            controlled = row["job_status"] in {"paused", "cancelled"}
            if status == "verification":
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='verifying',current_phase='verification',updated_at=now() WHERE id=%s",
                        (row["job_id"],),
                    )
                self._event(connection, row["job_id"], step_id, "review_approved", {})
            elif status == "changes_requested":
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='running',current_phase='coding',updated_at=now() WHERE id=%s",
                        (row["job_id"],),
                    )
                self._event(connection, row["job_id"], step_id, "changes_requested", {})
            elif status in {"blocked", "needs_human"}:
                job_status = "needs_human" if status == "needs_human" else "blocked"
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status=%s,current_phase=%s,updated_at=now() WHERE id=%s",
                        (job_status, status, row["job_id"]),
                    )
            return status

    # ------------------------------------------------------------------
    # Verification and checkpoint hand-offs
    # ------------------------------------------------------------------
    def claim_verification(self, worker_id: str, lease_seconds: int,
                           repository_lock_seconds: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.*,j.goal,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE s.status='verification' AND j.status IN ('verifying','running')
                  AND (s.orchestrator_lease_expires_at IS NULL
                       OR s.orchestrator_lease_expires_at < now())
                ORDER BY j.priority DESC,s.id FOR UPDATE OF s,j SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            owner = f"{worker_id}:verify:{row['id']}"
            if not self._acquire_repository_lock(
                connection, row["repository"], owner, repository_lock_seconds
            ):
                return None
            connection.execute(
                """UPDATE steps SET orchestrator_worker_id=%s,
                orchestrator_lease_expires_at=now()+(%s*interval '1 second'),updated_at=now()
                WHERE id=%s""", (worker_id, lease_seconds, row["id"])
            )
            connection.execute(
                "UPDATE jobs SET status='verifying',current_phase='verification',updated_at=now() WHERE id=%s",
                (row["job_id"],),
            )
            self._event(connection, row["job_id"], row["id"], "verification_started", {
                "worker_id": worker_id,
            })
            return row

    def record_verification(self, work: dict[str, Any], worker_id: str, result: Any) -> str:
        """Persist verifier evidence and select the next deterministic state."""
        commands = [self._command_payload(command) for command in getattr(result, "commands", [])]
        details = {
            "summary": getattr(result, "summary", ""),
            "secret_hits": list(getattr(result, "secret_hits", []) or []),
            "diff_check": getattr(result, "diff_check", None),
            "skipped": bool(getattr(result, "skipped", False)),
        }
        passed = bool(getattr(result, "passed", False))
        service_error = bool(getattr(result, "service_error", False))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.id=%s FOR UPDATE OF s,j", (work["id"],)
            ).fetchone()
            if not row:
                raise KeyError(work["id"])
            row = dict(row)
            if row.get("orchestrator_worker_id") != worker_id:
                raise RuntimeError("verification lease is no longer owned")
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 AS n FROM verification_runs WHERE step_id=%s",
                (work["id"],),
            ).fetchone()["n"]
            connection.execute(
                """INSERT INTO verification_runs(job_id,step_id,attempt,worker_id,status,commands,details,completed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,now())""",
                (work["job_id"], work["id"], attempt, worker_id,
                 "blocked" if service_error else ("passed" if passed else "failed"),
                 json.dumps(commands), json.dumps(details, default=str)),
            )
            for command in commands:
                connection.execute(
                    """INSERT INTO command_runs(step_id,argv,stdout,stderr,exit_code,duration_seconds,timed_out,source,attempt)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'orchestrator-verification',%s)""",
                    (work["id"], json.dumps(command["argv"]), command["stdout"], command["stderr"],
                     command["exit_code"], command["duration_seconds"], command["timed_out"], attempt),
                )
            controlled = row["job_status"] in {"paused", "cancelled"}
            if service_error:
                connection.execute(
                    """UPDATE steps SET status='blocked',blocker=%s,verification_result=%s,
                    orchestrator_worker_id=NULL,orchestrator_lease_expires_at=NULL,updated_at=now()
                    WHERE id=%s""",
                    (details["summary"][:30_000], json.dumps(details, default=str), work["id"]),
                )
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='blocked',current_phase='blocked',updated_at=now() WHERE id=%s",
                        (work["job_id"],),
                    )
                self._event(connection, work["job_id"], work["id"], "job_blocked", {
                    "phase": "verification", "reason": details["summary"][:30_000],
                })
                return "blocked"
            if passed:
                connection.execute(
                    """UPDATE steps SET status='checkpoint',verification_result=%s,verified_at=now(),
                    approved_files=files_changed,orchestrator_worker_id=NULL,
                    orchestrator_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (json.dumps(details, default=str), work["id"]),
                )
                if not controlled:
                    connection.execute(
                        "UPDATE jobs SET status='checkpointing',current_phase='checkpoint',updated_at=now() WHERE id=%s",
                        (work["job_id"],),
                    )
                self._event(connection, work["job_id"], work["id"], "verification_passed", {
                    "attempt": attempt, "commands": len(commands),
                })
                return "checkpoint"
            feedback = row.get("reviewer_feedback") or {}
            if not isinstance(feedback, dict):
                feedback = {"prior_feedback": feedback}
            feedback["verification_failure"] = details
            connection.execute(
                """UPDATE steps SET status='changes_requested',reviewer_feedback=%s,
                orchestrator_worker_id=NULL,orchestrator_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                (json.dumps(feedback, default=str), work["id"]),
            )
            if not controlled:
                connection.execute(
                    "UPDATE jobs SET status='running',current_phase='coding',updated_at=now() WHERE id=%s",
                    (work["job_id"],),
                )
            self._event(connection, work["job_id"], work["id"], "verification_failed", {
                "attempt": attempt, "summary": details["summary"],
            })
            return "changes_requested"

    def claim_checkpoint(self, worker_id: str, lease_seconds: int,
                         repository_lock_seconds: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.*,j.goal,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE s.status='checkpoint' AND j.status IN ('checkpointing','running')
                  AND (s.orchestrator_lease_expires_at IS NULL
                       OR s.orchestrator_lease_expires_at < now())
                ORDER BY j.priority DESC,s.id FOR UPDATE OF s,j SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            owner = f"{worker_id}:checkpoint:{row['id']}"
            if not self._acquire_repository_lock(
                connection, row["repository"], owner, repository_lock_seconds
            ):
                return None
            checkpoint = connection.execute(
                "SELECT * FROM checkpoint_runs WHERE step_id=%s FOR UPDATE", (row["id"],)
            ).fetchone()
            if checkpoint is None:
                marker = f"autonomous-step:{row['job_id']}:{row['id']}:{uuid.uuid4()}"
                connection.execute(
                    """INSERT INTO checkpoint_runs(step_id,job_id,marker,worker_id,status,approved_files,starting_commit)
                    VALUES(%s,%s,%s,%s,'running',%s,%s)""",
                    (row["id"], row["job_id"], marker, worker_id,
                     json.dumps(self._json_list(row["approved_files"]) or self._json_list(row["files_changed"])),
                     row.get("starting_commit")),
                )
            else:
                marker = checkpoint["marker"]
                connection.execute(
                    "UPDATE checkpoint_runs SET worker_id=%s,status='running',updated_at=now() WHERE step_id=%s",
                    (worker_id, row["id"]),
                )
            connection.execute(
                """UPDATE steps SET checkpoint_marker=%s,checkpoint_started_at=COALESCE(checkpoint_started_at,now()),
                orchestrator_worker_id=%s,orchestrator_lease_expires_at=now()+(%s*interval '1 second'),
                updated_at=now() WHERE id=%s""",
                (marker, worker_id, lease_seconds, row["id"]),
            )
            connection.execute(
                "UPDATE jobs SET status='checkpointing',current_phase='checkpoint',updated_at=now() WHERE id=%s",
                (row["job_id"],),
            )
            self._event(connection, row["job_id"], row["id"], "checkpoint_started", {
                "worker_id": worker_id, "marker": marker,
            })
            row["checkpoint_marker"] = marker
            row["approved_files"] = self._json_list(row["approved_files"]) or self._json_list(row["files_changed"])
            return row

    def record_checkpoint(self, work: dict[str, Any], worker_id: str, result: Any) -> str:
        success = bool(getattr(result, "success", False))
        commit_sha = getattr(result, "commit_sha", None)
        error = getattr(result, "error", None) or getattr(result, "summary", "checkpoint failed")
        retryable = bool(getattr(result, "retryable", False))
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE s.id=%s FOR UPDATE OF s,j""", (work["id"],)
            ).fetchone()
            if not row:
                raise KeyError(work["id"])
            if row.get("orchestrator_worker_id") != worker_id:
                raise RuntimeError("checkpoint lease is no longer owned")
            if success and not commit_sha:
                raise RuntimeError("checkpoint reported success without a commit SHA")
            controlled = row["job_status"] in {"paused", "cancelled"}
            if success:
                connection.execute(
                    """UPDATE checkpoint_runs SET status='complete',commit_sha=%s,error=NULL,
                    updated_at=now(),completed_at=now() WHERE step_id=%s""",
                    (commit_sha, work["id"]),
                )
                connection.execute(
                    """UPDATE steps SET status='complete',resulting_commit=%s,completed_at=now(),
                    orchestrator_worker_id=NULL,orchestrator_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (commit_sha, work["id"]),
                )
                if controlled:
                    connection.execute(
                        "UPDATE jobs SET current_phase='planning',current_step=NULL,updated_at=now() WHERE id=%s",
                        (work["job_id"],),
                    )
                else:
                    connection.execute(
                        """UPDATE jobs SET status='running',current_phase='planning',current_step=NULL,
                        updated_at=now() WHERE id=%s""", (work["job_id"],)
                    )
                self._event(connection, work["job_id"], work["id"], "checkpoint_created", {
                    "commit_sha": commit_sha, "marker": work["checkpoint_marker"],
                })
                self._event(connection, work["job_id"], work["id"], "step_completed", {
                    "commit_sha": commit_sha,
                })
                self._event(connection, work["job_id"], None, "planning_resumed", {})
                return "complete"
            # V1 autonomous policy: checkpoint failures never require routine
            # human intervention. Return the step to coding with the checkpoint
            # failure attached as reviewer feedback.
            next_step_status = "changes_requested"
            next_job_status = "running"
            connection.execute(
                """UPDATE checkpoint_runs SET status='failed',error=%s,updated_at=now()
                WHERE step_id=%s""", (str(error)[:30_000], work["id"])
            )
            feedback = {"checkpoint_failure": str(error)[:30_000]}
            connection.execute(
                """UPDATE steps SET status=%s,reviewer_feedback=COALESCE(reviewer_feedback,'{}'::jsonb) || %s::jsonb,
                orchestrator_worker_id=NULL,orchestrator_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                (next_step_status, json.dumps(feedback), work["id"]),
            )
            if not controlled:
                connection.execute(
                    "UPDATE jobs SET status=%s,current_phase=%s,updated_at=now() WHERE id=%s",
                    (next_job_status, "coding", work["job_id"]),
                )
            self._event(connection, work["job_id"], work["id"], "checkpoint_failed", {
                "error": str(error)[:30_000], "retryable": retryable,
            })
            return next_step_status

    @staticmethod
    def _command_payload(command: Any) -> dict[str, Any]:
        if isinstance(command, dict):
            source = command
            get = source.get
        else:
            get = lambda name, default=None: getattr(command, name, default)
        return {
            "argv": [str(arg) for arg in (get("argv", []) or [])],
            "stdout": str(get("stdout", ""))[:100_000],
            "stderr": str(get("stderr", ""))[:100_000],
            "exit_code": int(get("exit_code", 1)),
            "duration_seconds": float(get("duration_seconds", 0.0)),
            "timed_out": bool(get("timed_out", False)),
        }

    # ------------------------------------------------------------------
    # Recovery, controls, and observability
    # ------------------------------------------------------------------
    def enforce_iteration_limit(self) -> int | None:
        """Stop a job at its configured ceiling instead of idling forever."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT j.id FROM jobs j
                WHERE j.status IN ('pending','planning','running')
                  AND j.iteration_count >= j.max_iterations
                  AND NOT EXISTS (
                    SELECT 1 FROM steps s WHERE s.job_id=j.id AND s.status IN
                    ('queued','running','review','changes_requested','verification','checkpoint')
                  )
                ORDER BY j.priority DESC,j.id FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """UPDATE jobs SET status='blocked',current_phase='blocked',
                human_notes=COALESCE(human_notes || E'\n','') ||
                  'Configured maximum planning iterations reached.',
                updated_at=now() WHERE id=%s""", (row["id"],)
            )
            self._event(connection, row["id"], None, "iteration_limit_reached", {
                "reason": "Configured maximum planning iterations reached.",
            })
            return int(row["id"])

    def recover_expired(self) -> list[dict[str, Any]]:
        """Return expired work without blindly replaying potentially mutating actions.

        Verification and checkpoint work is reclaimable (the checkpoint marker
        makes the latter idempotent).  A Coder left mid-edit is returned to the
        caller for repository inspection rather than automatically re-run.
        """
        with self.connect() as connection:
            connection.execute("DELETE FROM repository_locks WHERE lease_expires_at < now()")
            rows = list(connection.execute(
                """SELECT s.id,s.job_id,s.repository,s.status,s.worker_id,s.starting_commit,
                s.lease_expires_at,s.orchestrator_worker_id,s.orchestrator_lease_expires_at
                FROM steps s JOIN jobs j ON j.id=s.job_id
                WHERE j.status NOT IN ('paused','cancelled','complete') AND (
                  (s.status='running' AND s.lease_expires_at < now()) OR
                  (s.status IN ('verification','checkpoint') AND s.orchestrator_lease_expires_at < now()) OR
                  (s.status='review' AND s.review_lease_expires_at < now())
                ) ORDER BY s.id"""
            ).fetchall())
            for row in rows:
                if row["status"] == "review":
                    connection.execute(
                        "UPDATE steps SET reviewer_worker_id=NULL,review_lease_expires_at=NULL,updated_at=now() WHERE id=%s",
                        (row["id"],),
                    )
                elif row["status"] in {"verification", "checkpoint"}:
                    connection.execute(
                        """UPDATE steps SET orchestrator_worker_id=NULL,
                        orchestrator_lease_expires_at=NULL,updated_at=now() WHERE id=%s""", (row["id"],)
                    )
                self._event(connection, row["job_id"], row["id"], "expired_lease_detected", {
                    "phase": row["status"], "worker_id": row.get("worker_id") or row.get("orchestrator_worker_id"),
                })
            return [dict(row) for row in rows]

    def safely_requeue_abandoned_coding(self, step_id: int, evidence: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.id=%s FOR UPDATE OF s,j", (step_id,)
            ).fetchone()
            if not row or row["status"] != "running":
                return
            connection.execute(
                """UPDATE steps SET status='queued',worker_id=NULL,lease_expires_at=NULL,
                blocker=NULL,updated_at=now() WHERE id=%s""", (step_id,)
            )
            connection.execute(
                "UPDATE jobs SET status='running',current_phase='coding',updated_at=now() WHERE id=%s",
                (row["job_id"],),
            )
            self._event(connection, row["job_id"], step_id, "coding_recovered", {"evidence": evidence})

    def block_abandoned_coding(self, step_id: int, evidence: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*,j.status AS job_status FROM steps s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.id=%s FOR UPDATE OF s,j", (step_id,)
            ).fetchone()
            if not row or row["status"] != "running":
                return
            connection.execute(
                """UPDATE steps SET status='blocked',blocker=%s,worker_id=NULL,lease_expires_at=NULL,
                updated_at=now() WHERE id=%s""", (evidence[:30_000], step_id)
            )
            connection.execute(
                "UPDATE jobs SET status='blocked',current_phase='blocked',updated_at=now() WHERE id=%s",
                (row["job_id"],),
            )
            self._event(connection, row["job_id"], step_id, "job_blocked", {"reason": evidence[:30_000]})

    def control(self, job_id: int, action: str) -> None:
        mapping = {"pause": "paused", "resume": "running", "cancel": "cancelled"}
        if action not in mapping:
            raise ValueError(f"unknown control action: {action}")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,status FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"job {job_id} does not exist")
            allowed = self.allowed_control_actions(row["status"])
            if action not in allowed:
                raise ValueError(
                    f"cannot {action} job {job_id} while status is {row['status']}"
                )
            status = mapping[action]
            paused = "now()" if status == "paused" else "NULL"
            connection.execute(
                f"""UPDATE jobs SET status=%s,planner_worker_id=NULL,planner_lease_expires_at=NULL,
                paused_at={paused},updated_at=now() WHERE id=%s""", (status, job_id)
            )
            event = {"pause": "job_paused", "resume": "job_resumed", "cancel": "job_cancelled"}[action]
            self._event(connection, job_id, None, event, {
                "status": status,
            })

    @staticmethod
    def allowed_control_actions(status: str) -> set[str]:
        """Return safe operator actions for one durable job state."""
        active = {"pending", "planning", "running", "reviewing", "verifying", "checkpointing"}
        resumable = {"paused", "blocked", "failed"}
        actions: set[str] = set()
        if status in active:
            actions.add("pause")
        if status in resumable:
            actions.add("resume")
        if status not in {"complete", "cancelled"}:
            actions.add("cancel")
        return actions

    def record_model_invocation(
        self,
        *,
        job_id: int | None,
        step_id: int | None,
        caller_agent: str,
        provider: str,
        model: str | None,
        route_reason: str | None,
        attempt: int | None,
        latency_seconds: float | None,
        usage: dict[str, Any] | None,
        estimated_cloud_cost: float | None = None,
        fallback: bool = False,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO llm_invocations(job_id,step_id,caller_agent,provider,model,route_reason,
                attempt,latency_seconds,usage,estimated_cloud_cost,fallback)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (job_id, step_id, caller_agent, provider, model, route_reason, attempt,
                 latency_seconds, json.dumps(usage or {}), estimated_cloud_cost, fallback),
            )
            if job_id is not None and (fallback or provider == "openrouter"):
                event_type = "model_escalated" if provider == "openrouter" else "model_route_fallback"
                self._event(connection, job_id, step_id, event_type, {
                    "caller_agent": caller_agent,
                    "provider": provider,
                    "model": model,
                    "reason": route_reason,
                    "attempt": attempt,
                    "fallback": fallback,
                    "estimated_cloud_cost": estimated_cloud_cost,
                })

    def heartbeat_worker(self, worker_id: str, component: str, status: str,
                         *, job_id: int | None = None, step_id: int | None = None,
                         action: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute("""INSERT INTO worker_heartbeats
            (worker_id,component,status,current_job_id,current_step_id,current_action,metadata)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(worker_id) DO UPDATE SET component=EXCLUDED.component,
            status=EXCLUDED.status,current_job_id=EXCLUDED.current_job_id,
            current_step_id=EXCLUDED.current_step_id,current_action=EXCLUDED.current_action,
            heartbeat_at=now(),metadata=EXCLUDED.metadata""",
            (worker_id, component, status, job_id, step_id, action,
             json.dumps(metadata or {}, default=str)))

    def answer(self, job_id: int, answer: str) -> None:
        note = str(answer).strip()
        if not note:
            raise ValueError("answer is required")
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            connection.execute("""UPDATE jobs SET status='running', current_phase='planning',
                planner_worker_id=NULL, planner_lease_expires_at=NULL,
                human_notes=COALESCE(human_notes || E'\\n','') || %s, updated_at=now() WHERE id=%s""",
                (note, job_id))
            self._event(connection, job_id, None, "human_answer_recorded", {"answer": note[:2000]})
