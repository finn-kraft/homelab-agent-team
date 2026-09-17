from __future__ import annotations
from orchestrator.store import OrchestratorStore
from planner_agent.store import PlannerStore

class ControlStore:
    def __init__(self, database_url):
        self.workflow = OrchestratorStore(database_url)
        self.planner = PlannerStore(database_url)
    def overview(self):
        with self.workflow.connect() as connection:
            agents = list(connection.execute("""SELECT agent,max(created_at) latest_activity,
            (array_agg(event_type ORDER BY created_at DESC))[1] latest_event
            FROM events GROUP BY agent ORDER BY agent""").fetchall())
            inference = connection.execute("""SELECT
            count(*) FILTER(WHERE provider='openrouter') cloud_requests,
            count(*) FILTER(WHERE provider<>'openrouter') local_requests,
            count(*) FILTER(WHERE fallback) fallback_requests,
            COALESCE(sum(estimated_cloud_cost),0) estimated_cloud_spend
            FROM llm_invocations""").fetchone()
            locks = list(connection.execute(
                "SELECT repository,owner,lease_expires_at,heartbeat_at FROM repository_locks ORDER BY repository"
            ).fetchall())
        return {"jobs": self.workflow.status(), "agents": [dict(x) for x in agents],
                "inference": dict(inference), "repository_locks": [dict(x) for x in locks]}
    def job(self, job_id): return self.workflow.inspect(job_id)
    def events(self, filters):
        clauses, values = [], []
        for key in ("job_id", "step_id", "agent", "event_type"):
            if filters.get(key): clauses.append(f"{key}=%s"); values.append(filters[key])
        if filters.get("failures"):
            clauses.append("(event_type LIKE '%%failed%%' OR event_type LIKE '%%blocked%%' OR event_type LIKE '%%crashed%%')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.workflow.connect() as connection:
            return [dict(x) for x in connection.execute(
                "SELECT * FROM events" + where + " ORDER BY id DESC LIMIT 500", values
            ).fetchall()]
    def create(self, data):
        return self.planner.create_job(data["goal"], data["repository"], data["branch"],
                                       int(data.get("priority", 0)), int(data.get("max_iterations", 100)))
    def action(self, job_id, action): self.workflow.control(job_id, action)
