from __future__ import annotations

import json
from typing import Any

from agent_core.models import PlannerDecision
from .decision import InvalidDecision, completion_is_supported, parse_decision
from .inspector import InspectionError, ReadOnlyRepositoryInspector
from .prompt import SYSTEM_PROMPT


class PlannerAgent:
    def __init__(self, store, router, inspector: ReadOnlyRepositoryInspector,
                 worker_id: str, lease_seconds: int = 300, decision_retries: int = 2,
                 escalation_attempt: int = 3):
        self.store, self.router, self.inspector = store, router, inspector
        self.worker_id, self.lease_seconds = worker_id, lease_seconds
        self.decision_retries, self.escalation_attempt = decision_retries, escalation_attempt

    def plan_once(self) -> PlannerDecision | None:
        job = self.store.claim_job(self.worker_id, self.lease_seconds)
        if not job:
            return None
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
            backend = self.router.choose(failed_attempts,
                                         failed_attempts >= self.escalation_attempt)
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(context, default=str)}]
            decision = None
            for _ in range(self.decision_retries + 1):
                self.store.heartbeat(job.id, self.worker_id, self.lease_seconds)
                response = backend.complete(messages)
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
                decision = PlannerDecision("blocked", "blocked",
                                           "The planning model repeatedly returned an unsafe or invalid decision.",
                                           blocker="invalid model decisions")
            self.store.apply_decision(job, self.worker_id, decision)
            return decision
        except (InspectionError, RuntimeError, ValueError) as exc:
            decision = PlannerDecision("blocked", "blocked",
                                       "Planning could not safely continue.", blocker=str(exc))
            self.store.apply_decision(job, self.worker_id, decision)
            return decision

    @staticmethod
    def _serializable(job) -> dict[str, Any]:
        return {name: getattr(job, name) for name in job.__slots__}

