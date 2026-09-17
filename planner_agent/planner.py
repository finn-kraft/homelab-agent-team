from __future__ import annotations

import json
from typing import Any

from agent_core.models import PlannerDecision
from agent_core.prompt_budget import bounded_messages, bounded_text, message_chars
from agent_core.llm import BackendError
from .decision import InvalidDecision, completion_is_supported, parse_decision
from .inspector import InspectionError, ReadOnlyRepositoryInspector
from .prompt import SYSTEM_PROMPT


class PlannerAgent:
    def __init__(self, store, router, inspector: ReadOnlyRepositoryInspector,
                 worker_id: str, lease_seconds: int = 300, decision_retries: int = 3,
                 escalation_attempt: int = 4, max_context_chars: int = 120_000,
                 max_package_steps: int = 3):
        self.store, self.router, self.inspector = store, router, inspector
        self.worker_id, self.lease_seconds = worker_id, lease_seconds
        self.decision_retries, self.escalation_attempt = decision_retries, escalation_attempt
        self.max_context_chars = max(16_000, int(max_context_chars))
        self.max_package_steps = max(1, min(5, int(max_package_steps)))
        self.last_prompt_chars = 0
        # Observability only: the deterministic Orchestrator uses this after a
        # bounded call to persist model-routing metadata without scraping logs.
        self.last_job_id: int | None = None

    def plan_once(self) -> PlannerDecision | None:
        self.last_job_id = None
        self.last_prompt_chars = 0
        job = self.store.claim_job(self.worker_id, self.lease_seconds)
        if not job:
            return None
        self.last_job_id = job.id
        try:
            job, steps, events = self.store.context(job.id)
            repository = self.inspector.inspect(job.repository, job.branch)
            if not repository["branch_matches"]:
                decision = PlannerDecision(
                    "blocked",
                    "blocked",
                    "The configured worker branch is not currently checked out.",
                    blocker=(
                        f"Expected branch {job.branch!r}; "
                        f"current branch is {repository['actual_branch']!r}."
                    ),
                )
                self.store.apply_decision(job, self.worker_id, decision)
                return decision
            context = {
                "job": self._serializable(job),
                "repository_evidence": self._bounded_repository(repository),
                "steps": self._bounded_steps(steps),
                "recent_events": self._bounded_events(events),
                "rules": {
                    "one_active_package_maximum": True,
                    "bounded_package_max_steps": self.max_package_steps,
                    "planner_is_read_only": True,
                    "completion_requires_approved_commits_and_multiple_evidence_items": True,
                },
            }
            failed_attempts = max(
                (int(step.get("attempt_count", 0)) for step in steps
                 if step.get("status") in {"failed", "blocked"}), default=0
            )
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": self._context_json(context)}]
            decision = None
            backend_errors = []
            for repair_attempt in range(self.decision_retries + 1):
                self.store.heartbeat(job.id, self.worker_id, self.lease_seconds)
                try:
                    route_attempt = failed_attempts + repair_attempt + 1
                    backend = self.router.choose(
                        route_attempt, route_attempt >= self.escalation_attempt)
                    request_messages = bounded_messages(messages, self.max_context_chars)
                    self.last_prompt_chars = message_chars(request_messages)
                    response = backend.complete(request_messages)
                except BackendError as exc:
                    backend_errors.append(str(exc))
                    messages.append({
                        "role": "user",
                        "content": "Inference failed transiently. Retry the same JSON contract.",
                    })
                    continue
                try:
                    candidate = parse_decision(response.text)
                    if len(candidate.steps or []) > self.max_package_steps:
                        raise InvalidDecision(
                            f"bounded package may contain at most {self.max_package_steps} steps"
                        )
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
        except Exception as exc:
            # Inspector subprocess timeouts and unexpected serialization/database
            # errors must release the planning lease just like known failures.
            # Leaving it owned would make an otherwise recoverable job appear
            # stuck until the full lease expires.
            self.store.defer(job.id, self.worker_id, "planner_internal_error", str(exc))
            return PlannerDecision("blocked", "running", "Planner failure will retry.",
                                   blocker=str(exc))

    def _context_json(self, context: dict[str, Any]) -> str:
        encoded = json.dumps(context, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        compact = {
            "job": context.get("job", {}),
            "repository_evidence": {
                "configured_branch": context.get("repository_evidence", {}).get("configured_branch"),
                "actual_branch": context.get("repository_evidence", {}).get("actual_branch"),
                "branch_matches": context.get("repository_evidence", {}).get("branch_matches"),
                "head": context.get("repository_evidence", {}).get("head"),
                "git_status": context.get("repository_evidence", {}).get("git_status", [])[:50],
                "recent_history": context.get("repository_evidence", {}).get("recent_history", [])[:10],
                "documents": {
                    name: self._bounded_text(value, 4_000)
                    for name, value in context.get("repository_evidence", {}).get("documents", {}).items()
                },
                "repository_files": context.get("repository_evidence", {}).get("repository_files", [])[:200],
            },
            "steps": context.get("steps", []),
            "recent_events": context.get("recent_events", [])[-5:],
            "rules": context.get("rules", {}),
            "context_notice": "Large planning history was truncated; inspect the repository and active steps directly.",
        }
        # The compact form is deliberately structured JSON. The configured
        # budget is large enough for bounded repository metadata and steps;
        # retain valid JSON if a caller supplies pathological metadata.
        encoded = json.dumps(compact, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        return json.dumps({
            "job_id": context.get("job", {}).get("id"),
            "step_count": len(context.get("steps", [])),
            "event_count": len(context.get("recent_events", [])),
            "context_notice": "Planning context was truncated to stay within the model budget.",
        }, default=str)

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        return bounded_text(value, limit)

    @classmethod
    def _bounded_repository(cls, repository: dict[str, Any]) -> dict[str, Any]:
        result = dict(repository)
        result["documents"] = {
            str(name): cls._bounded_text(value, 10_000)
            for name, value in dict(repository.get("documents") or {}).items()
        }
        result["repository_files"] = list(repository.get("repository_files") or [])[:500]
        return result

    @classmethod
    def _bounded_steps(cls, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bounded = []
        for step in steps:
            value = dict(step)
            for key in ("reviewer_feedback", "coder_response", "blocker"):
                if key in value:
                    value[key] = cls._bounded_text(value[key], 6_000)
            bounded.append(value)
        return bounded

    @classmethod
    def _bounded_events(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bounded = []
        for event in list(events)[:20]:
            value = dict(event)
            if "structured_payload" in value:
                value["structured_payload"] = cls._bounded_text(value["structured_payload"], 3_000)
            bounded.append(value)
        return bounded

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
