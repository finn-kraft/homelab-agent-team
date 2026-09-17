from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from orchestrator.store import OrchestratorStore
from planner_agent.store import PlannerStore
from orchestrator.worktrees import WorktreeManager

class ControlStore:
    def __init__(self, database_url, projects):
        self.workflow, self.planner, self.projects = (
            OrchestratorStore(database_url), PlannerStore(database_url), projects)
    def healthy(self):
        report = self.health()
        return bool(report.get("ready"))

    @staticmethod
    def _lease_expired(value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
        if not isinstance(value, datetime):
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value < datetime.now(timezone.utc)

    def health(self):
        readiness = getattr(self.workflow, "schema_readiness", None)
        if readiness is not None:
            report = readiness() if callable(readiness) else readiness
            # schema_readiness deliberately converts connection failures into a
            # report, so preserve that distinction for operators and probes.
            return {
                "database": "unavailable" if report.get("error") else "reachable",
                **report,
            }
        try:
            with self.workflow.connect() as connection:
                ok = connection.execute("SELECT 1 ok").fetchone()["ok"] == 1
                return {"ready": ok, "database": "reachable" if ok else "unavailable",
                        "migration_required": not ok}
        except Exception as exc:
            return {"ready": False, "database": "unavailable",
                    "migration_required": True, "error": str(exc)[:500]}
    def overview(self):
        with self.workflow.connect() as connection:
            workers = list(connection.execute("""SELECT *,
            heartbeat_at > now()-interval '30 seconds' online,
            EXTRACT(EPOCH FROM (now()-heartbeat_at))::double precision heartbeat_age_seconds
            FROM worker_heartbeats ORDER BY component,worker_id""").fetchall())
            agent_events = list(connection.execute("""SELECT DISTINCT ON(agent) agent,event_type,
            structured_payload,created_at FROM events ORDER BY agent,created_at DESC""").fetchall())
            active_work = list(connection.execute("""SELECT s.id step_id,s.job_id,s.status,s.title,
            s.attempt_count,s.files_changed,j.goal,j.current_phase,s.worker_id,
            s.lease_expires_at,s.reviewer_worker_id,s.review_lease_expires_at,
            s.orchestrator_worker_id,s.orchestrator_lease_expires_at,s.updated_at,
            s.started_at,r.verdict,
            m.provider,m.model,m.latency_seconds,p.progress_classification,
            lm.last_model_call_at, lm.model_call_age_seconds,
            pm.phase AS current_phase_name, pm.phase_started_at,
            EXTRACT(EPOCH FROM (now()-pm.phase_started_at))::double precision phase_duration_seconds,
            cr.current_command, cr.current_command_started_at,
            (SELECT count(*) FROM command_runs c WHERE c.step_id=s.id) command_count,
            (SELECT count(*) FROM review_issues i WHERE i.step_id=s.id AND i.status='open') open_issue_count
            FROM steps s JOIN jobs j ON j.id=s.job_id LEFT JOIN LATERAL
            (SELECT verdict FROM reviews WHERE step_id=s.id ORDER BY review_attempt DESC LIMIT 1) r ON true
            LEFT JOIN LATERAL (SELECT provider,model,latency_seconds FROM llm_invocations
            WHERE step_id=s.id ORDER BY id DESC LIMIT 1) m ON true
            LEFT JOIN LATERAL (SELECT structured_payload->>'progress_classification' progress_classification
            FROM events WHERE step_id=s.id AND event_type='agent_action' ORDER BY id DESC LIMIT 1) p ON true
            LEFT JOIN LATERAL (SELECT created_at AS last_model_call_at,
              EXTRACT(EPOCH FROM (now()-created_at))::double precision model_call_age_seconds
              FROM llm_invocations WHERE step_id=s.id ORDER BY id DESC LIMIT 1) lm ON true
            LEFT JOIN LATERAL (SELECT phase,started_at AS phase_started_at
              FROM phase_metrics WHERE step_id=s.id ORDER BY id DESC LIMIT 1) pm ON true
            LEFT JOIN LATERAL (SELECT argv AS current_command,created_at AS current_command_started_at
              FROM command_runs WHERE step_id=s.id ORDER BY id DESC LIMIT 1) cr ON true
            WHERE s.status NOT IN ('complete','cancelled') ORDER BY s.updated_at DESC""").fetchall())
            inference = connection.execute("""SELECT count(*) FILTER(WHERE provider='openrouter') cloud_requests,
            count(*) FILTER(WHERE provider<>'openrouter') local_requests,count(*) FILTER(WHERE fallback) fallback_requests,
            COALESCE(sum(estimated_cloud_cost),0) estimated_cloud_spend,
            avg(latency_seconds) FILTER(WHERE provider<>'openrouter') local_average_latency FROM llm_invocations""").fetchone()
            commits = list(connection.execute("""SELECT c.commit_sha,c.completed_at,c.approved_files,
            j.id job_id,j.goal,j.repository,s.id step_id,s.title,r.verdict reviewer_verdict,
            v.status verification_status FROM checkpoint_runs c JOIN jobs j ON j.id=c.job_id
            JOIN steps s ON s.id=c.step_id LEFT JOIN LATERAL
            (SELECT verdict FROM reviews WHERE step_id=s.id ORDER BY review_attempt DESC LIMIT 1) r ON true
            LEFT JOIN LATERAL (SELECT status FROM verification_runs WHERE step_id=s.id ORDER BY attempt DESC LIMIT 1) v ON true
            WHERE c.status='complete' ORDER BY c.completed_at DESC LIMIT 20""").fetchall())
            phase_metrics = list(connection.execute("""SELECT phase,count(*) AS runs,
            round(avg(duration_seconds)::numeric,2) AS average_seconds,
            round(max(duration_seconds)::numeric,2) AS max_seconds,
            round(avg(prompt_chars)::numeric,0) AS average_prompt_chars,
            max(prompt_chars) AS max_prompt_chars,
            round(avg(prompt_tokens)::numeric,0) AS average_prompt_tokens,
            max(prompt_tokens) AS max_prompt_tokens
            FROM phase_metrics GROUP BY phase ORDER BY phase""").fetchall())
            operations = list(connection.execute(
                "SELECT operation_id,job_id,action,status,requested_by,detail,error,created_at,updated_at,completed_at "
                "FROM control_operations ORDER BY created_at DESC LIMIT 100"
            ).fetchall())
        jobs = self.workflow.status()
        for work in active_work:
            status = work.get("status")
            lease = (work.get("lease_expires_at") if status == "running" else
                     work.get("review_lease_expires_at") if status == "review" else
                     work.get("orchestrator_lease_expires_at") if status in {"verification", "checkpoint"} else None)
            work["lease_owner"] = (work.get("worker_id") if status == "running" else
                                    work.get("reviewer_worker_id") if status == "review" else
                                    work.get("orchestrator_worker_id") if status in {"verification", "checkpoint"} else None)
            work["stale"] = self._lease_expired(lease)
            if work["stale"]:
                work["stale_warning"] = "Lease expired; use Recover lease to release it safely."
            elif work.get("model_call_age_seconds") is not None and work["model_call_age_seconds"] > 180:
                work["stale_warning"] = "No model call has been recorded for more than 3 minutes."
            else:
                work["stale_warning"] = None
        return {"jobs": jobs, "missions": self.workflow.list_missions(),
                "human_queue": self.workflow.list_human_queue("open"),
                "workers": [dict(x) for x in workers],
                "agent_events":[dict(x) for x in agent_events],"active_work":[dict(x) for x in active_work],
                "inference": dict(inference),
                "phase_metrics": [dict(x) for x in phase_metrics],
                "operations": [dict(x) for x in operations],
                "recent_commits": [dict(x) for x in commits],
                "attention_count": sum(j["status"] in {"needs_human","blocked","failed"} for j in jobs)}
    def project_list(self):
        jobs = self.workflow.status()
        by_repo = {j["repository"]:j for j in jobs if j["status"] not in {"complete","cancelled"}}
        result = self.projects.list()
        with self.workflow.connect() as connection:
            rows = connection.execute("""SELECT DISTINCT ON(j.repository) j.repository,c.commit_sha,
            c.completed_at,s.title FROM checkpoint_runs c JOIN jobs j ON j.id=c.job_id
            JOIN steps s ON s.id=c.step_id WHERE c.status='complete'
            ORDER BY j.repository,c.completed_at DESC""").fetchall()
            autonomous = {row["repository"]:dict(row) for row in rows}
        for project in result:
            project["current_job"] = by_repo.get(project["repository"])
            project["latest_autonomous_commit"] = autonomous.get(project["repository"])
        return result

    def missions(self):
        return self.workflow.list_missions()

    def mission(self, mission_id):
        result = self.workflow.mission_detail(int(mission_id))
        if result is None:
            raise KeyError(mission_id)
        return result

    def work_packages(self, mission_id=None):
        return self.workflow.list_work_packages(int(mission_id) if mission_id is not None else None)

    def human_queue(self, status="open"):
        return self.workflow.list_human_queue(status)

    def answer_human_request(self, request_id, answer):
        self.workflow.answer_human_request(int(request_id), answer)
    def job(self, job_id):
        result = self.workflow.inspect(job_id)
        with self.workflow.connect() as connection:
            for step in result["steps"]:
                sid = step["id"]
                step["reviews"] = [dict(x) for x in connection.execute(
                    "SELECT * FROM reviews WHERE step_id=%s ORDER BY review_attempt", (sid,)).fetchall()]
                step["verification_runs"] = [dict(x) for x in connection.execute(
                    "SELECT * FROM verification_runs WHERE step_id=%s ORDER BY attempt", (sid,)).fetchall()]
                step["commands"] = [dict(x) for x in connection.execute(
                    """SELECT argv,exit_code,duration_seconds,timed_out,cancelled,source,attempt,created_at
                    FROM command_runs WHERE step_id=%s ORDER BY id""", (sid,)).fetchall()]
                step["model_routes"] = [dict(x) for x in connection.execute(
                    """SELECT caller_agent,provider,model,route_reason,attempt,latency_seconds,usage,
                    estimated_cloud_cost,fallback,created_at FROM llm_invocations WHERE step_id=%s ORDER BY id""",
                    (sid,)).fetchall()]
                step["phase_metrics"] = [dict(x) for x in connection.execute(
                    "SELECT phase,status,duration_seconds,prompt_chars,prompt_tokens,context_sha256,provider,model,detail,started_at,completed_at "
                    "FROM phase_metrics WHERE step_id=%s ORDER BY id", (sid,)).fetchall()]
                progress = connection.execute(
                    """SELECT structured_payload->>'progress_classification' AS value
                    FROM events WHERE step_id=%s AND event_type='agent_action'
                    ORDER BY id DESC LIMIT 1""", (sid,)
                ).fetchone()
                step["progress_classification"] = progress["value"] if progress else None
                step["review_issues"] = [dict(x) for x in connection.execute(
                    "SELECT * FROM review_issues WHERE step_id=%s ORDER BY id", (sid,)).fetchall()]
                checkpoint = connection.execute("SELECT * FROM checkpoint_runs WHERE step_id=%s", (sid,)).fetchone()
                step["checkpoint"] = dict(checkpoint) if checkpoint else None
                status = step.get("status")
                lease = (step.get("lease_expires_at") if status == "running" else
                         step.get("review_lease_expires_at") if status == "review" else
                         step.get("orchestrator_lease_expires_at") if status in {"verification", "checkpoint"} else None)
                step["lease_owner"] = (step.get("worker_id") if status == "running" else
                                        step.get("reviewer_worker_id") if status == "review" else
                                        step.get("orchestrator_worker_id") if status in {"verification", "checkpoint"} else None)
                if step["lease_owner"]:
                    heartbeat = connection.execute(
                        """SELECT heartbeat_at,
                           EXTRACT(EPOCH FROM (now()-heartbeat_at))::double precision heartbeat_age_seconds
                           FROM worker_heartbeats WHERE worker_id=%s
                           ORDER BY heartbeat_at DESC LIMIT 1""",
                        (step["lease_owner"],),
                    ).fetchone()
                    if heartbeat:
                        step["heartbeat_at"] = heartbeat["heartbeat_at"]
                        step["heartbeat_age_seconds"] = heartbeat["heartbeat_age_seconds"]
                step["stale"] = self._lease_expired(lease)
                step["stale_warning"] = "Lease expired; use Recover lease to release it safely." if step["stale"] else None
        result["current_step_detail"] = next((s for s in result["steps"] if s["status"] != "complete"), None)
        result["needs_attention"] = self._attention(result)
        list_operations = getattr(self.workflow, "list_control_operations", None)
        result["operations"] = list_operations(int(job_id)) if list_operations else []
        return result
    @staticmethod
    def _attention(result):
        status = result["job"]["status"]
        if status not in {"needs_human", "blocked"}:
            return None

        # A blocked job is a technical stop, not a question for the operator.
        # Prefer the durable step blocker so the UI does not hide the useful
        # detail behind a generic "Provide instructions" message.
        current = result.get("current_step_detail")
        step_blocker = current.get("blocker") if current else None
        job_blocker = result["job"].get("blocker")
        if step_blocker or job_blocker:
            return {
                "reason": step_blocker or job_blocker or status,
                "question": (
                    "Resolve the technical blocker, then use Resume to let the "
                    "team reassess durable state."
                ),
                "context": {"step_blocker": step_blocker, "job_blocker": job_blocker},
                "event_id": None,
                "can_answer": False,
            }

        for event in result["events"]:
            payload = event.get("structured_payload") or {}
            if isinstance(payload, str):
                try: payload = json.loads(payload)
                except json.JSONDecodeError: payload = {}
            question = payload.get("human_question") or payload.get("question")
            reason = (payload.get("reason") or payload.get("blocker") or
                      payload.get("summary") or event["event_type"])
            if question or event["event_type"] in {"job_needs_human", "human_input_requested", "job_blocked"}:
                return {"reason":reason,
                        "question":question or (
                            "Resolve the technical blocker, then use Resume to let the "
                            "team reassess durable state."
                        ),
                        "context":payload,"event_id":event["id"],
                        "can_answer":status == "needs_human"}
        return {"reason":status,
                "question":(
                    "Provide a decision to continue."
                    if status == "needs_human" else
                    "Resolve the technical blocker, then use Resume to let the team reassess durable state."
                ),
                "context":{},"can_answer":status == "needs_human"}
    def events(self, filters):
        clauses, values = [], []
        for key in ("job_id","step_id","agent","event_type"):
            if filters.get(key): clauses.append(f"{key}=%s"); values.append(filters[key])
        if filters.get("failures"): clauses.append("(event_type LIKE '%%failed%%' OR event_type LIKE '%%blocked%%' OR event_type LIKE '%%crashed%%')")
        if filters.get("routing"): clauses.append("event_type IN ('model_escalated','model_route_fallback')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.workflow.connect() as connection:
            return [dict(x) for x in connection.execute(
                "SELECT * FROM events" + where + " ORDER BY id DESC LIMIT 500", values).fetchall()]
    def create(self, data):
        project = self.projects.get(data["project_id"]); goal = str(data["goal"]).strip()
        if len(goal) < 10 or len(goal) > 12_000: raise ValueError("goal must be 10-12000 characters")
        priority, max_iterations = int(data.get("priority",0)), int(data.get("max_iterations",100))
        if not -100 <= priority <= 100: raise ValueError("priority must be between -100 and 100")
        if not 1 <= max_iterations <= 10_000: raise ValueError("max_iterations must be 1-10000")
        return self.planner.create_job(goal, project.repository, project.branch,
                                       priority, max_iterations)
    def action(self, job_id, action): self.workflow.control(job_id, action)
    def begin_operation(self, job_id, action, requested_by="control-center"):
        return self.workflow.begin_control_operation(job_id, action, requested_by)
    def update_operation(self, operation_id, status, detail=None, error=None):
        return self.workflow.update_control_operation(operation_id, status, detail=detail, error=error)
    def operation(self, operation_id):
        return self.workflow.control_operation(operation_id)
    def operations(self, job_id=None, limit=100):
        return self.workflow.list_control_operations(job_id, limit)
    def retry(self, job_id):
        return self.workflow.retry_job(int(job_id))
    def recover_lease(self, job_id):
        return self.workflow.recover_job(int(job_id))
    def rerun_verification(self, job_id, step_id=None):
        return self.workflow.rerun_verification(int(job_id), int(step_id) if step_id is not None else None)
    def refresh_planning(self, job_id):
        return self.workflow.refresh_planning(int(job_id))
    def cleanup_worktrees(self):
        manager = WorktreeManager(os.getenv("ENGINEERING_WORKTREE_ROOT", "/tmp/agent-worktrees"))
        return self.workflow.reconcile_worktrees(manager, remove_orphans=True)
    def remove_job(self, job_id): self.workflow.remove_job(job_id)
    def answer(self, job_id, answer):
        answer = str(answer).strip()
        if not answer or len(answer) > 12_000:
            raise ValueError("answer must be 1-12000 characters")

        with self.workflow.connect() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()

            if not job:
                raise KeyError(job_id)

            if job["status"] != "needs_human":
                raise ValueError("job is not waiting for human input")

            connection.execute(
                """UPDATE jobs
                   SET status='running',
                       human_notes=concat_ws(E'\\n', human_notes, %s::text),
                       planner_worker_id=NULL,
                       planner_lease_expires_at=NULL,
                       updated_at=now()
                   WHERE id=%s""",
                (answer, job_id),
            )

            connection.execute(
                """UPDATE steps
                   SET status='changes_requested',
                       blocker=NULL,
                       updated_at=now()
                   WHERE job_id=%s
                     AND status='needs_human'""",
                (job_id,),
            )

            self.workflow._event(
                connection,
                job_id,
                job.get("current_step"),
                "human_response_received",
                {"answer": answer},
                agent="control-center",
            )
            connection.execute(
                """UPDATE human_queue SET status='answered',answer=%s,answered_by='control-center',
                   answered_at=now(),updated_at=now() WHERE job_id=%s AND status='open'""",
                (answer, job_id),
            )
