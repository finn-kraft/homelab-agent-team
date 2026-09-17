from __future__ import annotations
import json
from orchestrator.store import OrchestratorStore
from planner_agent.store import PlannerStore

class ControlStore:
    def __init__(self, database_url, projects):
        self.workflow, self.planner, self.projects = (
            OrchestratorStore(database_url), PlannerStore(database_url), projects)
    def healthy(self):
        report = self.health()
        return bool(report.get("ready"))

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
            workers = list(connection.execute("""SELECT *,heartbeat_at > now()-interval '30 seconds' online
            FROM worker_heartbeats ORDER BY component,worker_id""").fetchall())
            agent_events = list(connection.execute("""SELECT DISTINCT ON(agent) agent,event_type,
            structured_payload,created_at FROM events ORDER BY agent,created_at DESC""").fetchall())
            active_work = list(connection.execute("""SELECT s.id step_id,s.job_id,s.status,s.title,
            s.attempt_count,s.files_changed,j.goal,j.current_phase,r.verdict,
            m.provider,m.model,m.latency_seconds,p.progress_classification,
            (SELECT count(*) FROM command_runs c WHERE c.step_id=s.id) command_count,
            (SELECT count(*) FROM review_issues i WHERE i.step_id=s.id AND i.status='open') open_issue_count
            FROM steps s JOIN jobs j ON j.id=s.job_id LEFT JOIN LATERAL
            (SELECT verdict FROM reviews WHERE step_id=s.id ORDER BY review_attempt DESC LIMIT 1) r ON true
            LEFT JOIN LATERAL (SELECT provider,model,latency_seconds FROM llm_invocations
            WHERE step_id=s.id ORDER BY id DESC LIMIT 1) m ON true
            LEFT JOIN LATERAL (SELECT structured_payload->>'progress_classification' progress_classification
            FROM events WHERE step_id=s.id AND event_type='agent_action' ORDER BY id DESC LIMIT 1) p ON true
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
            max(prompt_chars) AS max_prompt_chars
            FROM phase_metrics GROUP BY phase ORDER BY phase""").fetchall())
        jobs = self.workflow.status()
        return {"jobs": jobs, "missions": self.workflow.list_missions(),
                "human_queue": self.workflow.list_human_queue("open"),
                "workers": [dict(x) for x in workers],
                "agent_events":[dict(x) for x in agent_events],"active_work":[dict(x) for x in active_work],
                "inference": dict(inference),
                "phase_metrics": [dict(x) for x in phase_metrics],
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
                    "SELECT phase,status,duration_seconds,prompt_chars,provider,model,detail,started_at,completed_at "
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
        result["current_step_detail"] = next((s for s in result["steps"] if s["status"] != "complete"), None)
        result["needs_attention"] = self._attention(result)
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
