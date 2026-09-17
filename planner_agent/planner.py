from __future__ import annotations

import json
from typing import Any

from agent_core.models import PlannerDecision
from agent_core.llm import BackendError
from .decision import InvalidDecision, completion_is_supported, parse_decision
from .inspector import InspectionError, ReadOnlyRepositoryInspector
from .prompt import SYSTEM_PROMPT


class PlannerAgent:
    def __init__(self, store, router, inspector: ReadOnlyRepositoryInspector,
                 worker_id: str, lease_seconds: int = 300, decision_retries: int = 3,
                 escalation_attempt: int = 4):
        self.store, self.router, self.inspector = store, router, inspector
        self.worker_id, self.lease_seconds = worker_id, lease_seconds
        self.decision_retries, self.escalation_attempt = decision_retries, escalation_attempt
        # Observability only: the deterministic Orchestrator uses this after a
        # bounded call to persist model-routing metadata without scraping logs.
        self.last_job_id: int | None = None

    def plan_once(self) -> PlannerDecision | None:
        job = self.store.claim_job(self.worker_id, self.lease_seconds)
        if not job:
            return None
        self.last_job_id = job.id
        try:
            job, steps, events = self.store.context(job.id)
            repository = self.inspector.inspect(job.repository, job.branch)
            if not repository["branch_matches"]:
                decision = PlannerDecision(
                    "needs_human", "needs_human",
                    "The configured worker branch is not currently checked out.",
                    human_question=(f"Check out safe branch {job.branch!r} in {job.repository!r} "
                                    f"before resuming; current branch is {repository['actual_branch']!r}."),
                )
                self.store.apply_decision(job, self.worker_id, decision)
                return decision
            context = {
                "job": self._serializable(job),
                "repository_evidence": repository,
                "steps": steps,
                "recent_events": events,
                "rules": {
                    "one_active_step_maximum": True,
                    "planner_is_read_only": True,
                    "completion_requires_approved_commits_and_multiple_evidence_items": True,
                },
            }
            failed_attempts = max(
                (int(step.get("attempt_count", 0)) for step in steps
                 if step.get("status") in {"failed", "blocked"}), default=0
            )
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(context, default=str)}]
            decision = None
            backend_errors = []
            for repair_attempt in range(self.decision_retries + 1):
                self.store.heartbeat(job.id, self.worker_id, self.lease_seconds)
                try:
                    route_attempt = failed_attempts + repair_attempt + 1
                    backend = self.router.choose(
                        route_attempt, route_attempt >= self.escalation_attempt)
                    response = backend.complete(messages)
                except BackendError as exc:
                    backend_errors.append(str(exc))
                    messages.append({
                        "role": "user",
                        "content": "Inference failed transiently. Retry the same JSON contract.",
                    })
                    continue
                try:
                    candidate = parse_decision(response.text)
                    if candidate.decision == "complete" and not completion_is_supported(candidate, steps):
                        raise InvalidDecision(
                            "completion is unsupported: every step needs an approved review and commit"
                        )
                    decision = candidate
                    decision.model, decision.provider = response.model, response.backend
                    decision.latency_seconds, decision.usage = response.latency_seconds, response.usage
                    break
                except InvalidDecision as exc:
                    messages.extend([
                        {"role": "assistant", "content": response.text},
                        {"role": "user", "content": f"Decision rejected: {exc}. Return corrected JSON."},
                    ])
            if decision is None:
                reason = "inference_unavailable" if backend_errors else "invalid_model_output"
                self.store.defer(
                    job.id, self.worker_id, reason,
                    backend_errors[-1] if backend_errors else "structured repair exhausted",
                )
                return PlannerDecision(
                    "blocked", "running",
                    "Planning retry scheduled; the persistent job remains recoverable.",
                    blocker=reason,
                )
            if decision.decision == "blocked" and self._recoverable_blocker(decision.blocker):
                self.store.defer(job.id, self.worker_id, "planner_blocker",
                                 json.dumps(decision.blocker, default=str))
                return PlannerDecision(
                    "blocked", "running", "Planning retry scheduled; the persistent job remains recoverable.",
                    blocker="planner_blocker",
                )
            self.store.apply_decision(job, self.worker_id, decision)
            return decision
        except InspectionError as exc:
            self.store.defer(job.id, self.worker_id, "repository_conflict", str(exc))
            return PlannerDecision("blocked", "running", "Repository inspection will retry.",
                                   blocker=str(exc))
        except (RuntimeError, ValueError) as exc:
            self.store.defer(job.id, self.worker_id, "planner_internal_error", str(exc))
            return PlannerDecision("blocked", "running", "Planner failure will retry.",
                                   blocker=str(exc))

    @staticmethod
    def _serializable(job) -> dict[str, Any]:
        return {name: getattr(job, name) for name in job.__slots__}

    @staticmethod
    def _recoverable_blocker(blocker: Any) -> bool:
        """Keep transient/environment blockers out of permanent job state."""
        value = json.dumps(blocker, default=str).lower()
        return any(term in value for term in (
            "unavailable", "timeout", "timed_out", "transient", "connection",
            "repository_conflict", "missing_external_resource",
        ))
