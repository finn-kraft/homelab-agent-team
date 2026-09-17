from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.models import Job, JobStatus, PlannerDecision, Step, StepStatus
from .decision import normalize_objective


class PlannerStore:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("install the project to enable PostgreSQL support") from exc
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def migrate(self) -> None:
        sql = Path(__file__).parent.parent.joinpath("agent_core", "schema.sql").read_text()
        with self.connect() as connection:
            connection.execute(sql)

    def create_job(self, goal: str, repository: str, branch: str, priority: int = 0,
                   max_iterations: int = 100) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO jobs(goal,repository,branch,priority,max_iterations)
                VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                (goal, repository, branch, priority, max_iterations),
            ).fetchone()
            job_id = row["id"]
            self._event(connection, job_id, None, "job_created", {"goal": goal})
            return job_id

    def claim_job(self, worker_id: str, lease_seconds: int) -> Job | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                  SELECT j.id FROM jobs j
                  WHERE j.status IN ('pending','planning','running')
                    AND j.iteration_count < j.max_iterations
                    AND (j.planner_lease_expires_at IS NULL OR j.planner_lease_expires_at < now())
                    AND NOT EXISTS (
                      SELECT 1 FROM steps s WHERE s.job_id=j.id
                      AND s.status IN ('queued','running','review','changes_requested','verification')
                    )
                  ORDER BY j.priority DESC, j.id FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE jobs j SET status='planning', planner_worker_id=%s,
                  planner_lease_expires_at=now()+(%s*interval '1 second'),
                  started_at=COALESCE(started_at,now()), updated_at=now()
                FROM candidate WHERE j.id=candidate.id RETURNING j.*
                """, (worker_id, lease_seconds),
            ).fetchone()
            if row:
                self._event(connection, row["id"], None, "job_planning_started",
                            {"worker_id": worker_id, "lease_seconds": lease_seconds})
            return self._job(row) if row else None

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE jobs SET planner_lease_expires_at=now()+(%s*interval '1 second')
                WHERE id=%s AND planner_worker_id=%s AND status='planning'""",
                (lease_seconds, job_id, worker_id),
            )
            return result.rowcount == 1

    def context(self, job_id: int) -> tuple[Job, list[dict[str, Any]], list[dict[str, Any]]]:
        with self.connect() as connection:
            job_row = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if not job_row:
                raise KeyError(job_id)
            steps = list(connection.execute(
                "SELECT * FROM steps WHERE job_id=%s ORDER BY sequence", (job_id,)
            ).fetchall())
            events = list(connection.execute(
                """SELECT event_type,step_id,structured_payload,created_at FROM events
                WHERE job_id=%s ORDER BY id DESC LIMIT 100""", (job_id,)
            ).fetchall())
            return self._job(job_row), [dict(row) for row in steps], [dict(row) for row in events]

    def apply_decision(self, job: Job, worker_id: str, decision: PlannerDecision) -> int | None:
        with self.connect() as connection:
            locked = connection.execute(
                "SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job.id,)
            ).fetchone()
            if not locked or locked["planner_worker_id"] != worker_id or locked["status"] != "planning":
                raise RuntimeError("planning lease is no longer owned")
            if decision.decision in {"create_step", "replace_step"}:
                active = connection.execute(
                    """SELECT 1 FROM steps WHERE job_id=%s AND status IN
                    ('queued','running','review','changes_requested','verification') LIMIT 1""", (job.id,)
                ).fetchone()
                if active:
                    raise RuntimeError("job already has an active implementation step")
                prior = connection.execute(
                    "SELECT objective FROM steps WHERE job_id=%s", (job.id,)
                ).fetchall()
                objective = str(decision.step["objective"])
                if normalize_objective(objective) in {normalize_objective(r["objective"]) for r in prior}:
                    raise RuntimeError("equivalent step already exists")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM steps WHERE job_id=%s", (job.id,)
                ).fetchone()["n"]
                step = decision.step
                row = connection.execute(
                    """INSERT INTO steps(job_id,sequence,repository,branch,title,objective,
                    rationale,acceptance_criteria,constraints,suggested_files,dependencies,assigned_agent)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (job.id, sequence, job.repository, job.branch, step["title"], objective,
                     step.get("rationale", ""), json.dumps(step["acceptance_criteria"]),
                     json.dumps(step["constraints"]), json.dumps(step["suggested_files"]),
                     json.dumps(step.get("dependencies", [])), step["assigned_agent"]),
                ).fetchone()
                step_id = row["id"]
                connection.execute(
                    """UPDATE jobs SET status='running',current_step=%s,iteration_count=iteration_count+1,
                    planner_worker_id=NULL,planner_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (step_id, job.id),
                )
                self._event(connection, job.id, step_id, "step_created", decision.as_dict())
                if decision.provider and decision.provider != "ollama":
                    self._event(connection, job.id, step_id, "model_escalated", {
                        "provider": decision.provider, "model": decision.model,
                        "reason": "configured failure/complexity escalation policy",
                    })
                return step_id
            status = {"complete": "complete", "needs_human": "needs_human",
                      "blocked": "blocked", "wait_for_review": "reviewing",
                      "retry_step": "running"}.get(decision.decision, "running")
            if decision.decision == "retry_step":
                failed = connection.execute(
                    """SELECT id FROM steps WHERE job_id=%s AND status IN ('failed','blocked')
                    ORDER BY sequence DESC LIMIT 1 FOR UPDATE""", (job.id,)
                ).fetchone()
                if not failed:
                    raise RuntimeError("retry requested without a failed step")
                connection.execute(
                    """UPDATE steps SET status='queued',blocker=NULL,updated_at=now()
                    WHERE id=%s""", (failed["id"],)
                )
            completed = ", completed_at=now()" if status == "complete" else ""
            connection.execute(
                f"""UPDATE jobs SET status=%s,iteration_count=iteration_count+1,
                planner_worker_id=NULL,planner_lease_expires_at=NULL,updated_at=now(){completed}
                WHERE id=%s""", (status, job.id),
            )
            self._event(connection, job.id, None, f"job_{decision.decision}", decision.as_dict())
            return None

    def defer(self, job_id: int, worker_id: str, failure_kind: str, detail: str,
              retry_seconds: int = 20) -> None:
        """Release a transient planning failure without permanently blocking the job."""
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE jobs SET status='running',planner_worker_id=NULL,
                planner_lease_expires_at=now()+(%s*interval '1 second'),updated_at=now()
                WHERE id=%s AND planner_worker_id=%s RETURNING id""",
                (retry_seconds, job_id, worker_id),
            ).fetchone()
            if not row:
                raise RuntimeError("planning lease is no longer owned")
            self._event(connection, job_id, None, "planning_retry_scheduled", {
                "failure_kind": failure_kind,
                "detail": detail[:30_000],
                "retry_seconds": retry_seconds,
            })

    def pause(self, job_id: int) -> None:
        self._set_control_status(job_id, "paused", "job_paused")

    def resume(self, job_id: int) -> None:
        self._set_control_status(job_id, "running", "job_resumed")

    def cancel(self, job_id: int) -> None:
        self._set_control_status(job_id, "cancelled", "job_cancelled")

    def _set_control_status(self, job_id: int, status: str, event: str) -> None:
        with self.connect() as connection:
            extra = ", paused_at=now()" if status == "paused" else ", paused_at=NULL"
            connection.execute(
                f"""UPDATE jobs SET status=%s,planner_worker_id=NULL,
                planner_lease_expires_at=NULL,updated_at=now(){extra} WHERE id=%s""",
                (status, job_id),
            )
            self._event(connection, job_id, None, event, {"status": status})

    @staticmethod
    def _event(connection, job_id: int, step_id: int | None,
               event_type: str, payload: dict) -> None:
        connection.execute(
            """INSERT INTO events(job_id,step_id,agent,event_type,structured_payload)
            VALUES(%s,%s,'planner-agent',%s,%s)""",
            (job_id, step_id, event_type, json.dumps(payload, default=str)),
        )

    @staticmethod
    def _job(row) -> Job:
        return Job(row["id"], row["goal"], row["repository"], row["branch"],
                   JobStatus(row["status"]), row["priority"], row["current_phase"],
                   row["current_step"], row["iteration_count"], row["max_iterations"],
                   row["human_notes"])
