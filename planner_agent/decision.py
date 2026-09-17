from __future__ import annotations

import json
import re
from typing import Any

from agent_core.models import PlannerDecision


VALID_DECISIONS = {"create_step", "retry_step", "replace_step", "wait_for_review",
                   "needs_human", "blocked", "complete"}
FORBIDDEN_INSTRUCTIONS = (
    "force push", "force-push", "git reset --hard", "bypass review",
    "ignore failing tests", "disable security", "push to main", "push to master",
)


class InvalidDecision(ValueError):
    pass


def normalize_objective(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", value.lower()).split())


def parse_decision(text: str) -> PlannerDecision:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidDecision("planner response is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("decision") not in VALID_DECISIONS:
        raise InvalidDecision("planner returned an unknown decision")
    if payload.get("decision") in {"create_step", "replace_step"}:
        required_top = {"decision", "job_status", "reasoning_summary", "step",
                        "evidence", "human_question", "blocker"}
        if set(payload) != required_top:
            raise InvalidDecision("step decision does not match the exact output contract")
    decision = PlannerDecision(
        decision=payload["decision"],
        job_status=str(payload.get("job_status", "running")),
        reasoning_summary=str(payload.get("reasoning_summary", "")),
        step=payload.get("step"), evidence=list(payload.get("evidence", [])),
        human_question=payload.get("human_question"), blocker=payload.get("blocker"),
    )
    validate_decision(decision)
    return decision


def validate_decision(decision: PlannerDecision) -> None:
    serialized = json.dumps(decision.as_dict()).lower()
    if any(phrase in serialized for phrase in FORBIDDEN_INSTRUCTIONS):
        raise InvalidDecision("decision attempts to bypass a team safety boundary")
    if decision.decision in {"create_step", "replace_step"}:
        if not isinstance(decision.step, dict):
            raise InvalidDecision("step-producing decision has no structured step")
        required = ("title", "objective", "acceptance_criteria", "constraints",
                    "suggested_files", "assigned_agent")
        if any(key not in decision.step for key in required):
            raise InvalidDecision("step contract is incomplete")
        criteria = decision.step["acceptance_criteria"]
        if not isinstance(criteria, list) or not criteria or not all(
                isinstance(item, str) and len(item.strip()) >= 8 for item in criteria):
            raise InvalidDecision("acceptance criteria must be concrete strings")
        if decision.step["assigned_agent"] != "coder-agent":
            raise InvalidDecision("MVP only assigns implementation to coder-agent")
    if decision.decision == "complete" and len(decision.evidence) < 2:
        raise InvalidDecision("overall completion requires multiple evidence items")
    if decision.decision == "needs_human" and not decision.human_question:
        raise InvalidDecision("needs_human requires a specific question")
    if decision.decision == "needs_human":
        hard_gate_terms = (
            "destructive", "irreversible", "credential", "secret", "contradict",
            "production", "deploy", "merge", "exhausted", "financial data",
        )
        basis = f"{decision.reasoning_summary} {decision.human_question}".lower()
        if not any(term in basis for term in hard_gate_terms):
            raise InvalidDecision("needs_human is reserved for a concrete hard gate")
    if decision.decision == "blocked":
        if decision.job_status not in {"blocked", "running"}:
            raise InvalidDecision("blocked decision has no valid job status")
        blocker = decision.blocker
        if not blocker:
            raise InvalidDecision("blocked requires a concrete technical blocker")
        if isinstance(blocker, dict) and str(blocker.get("type", "")).lower() in {
            "decision", "needs_human", "question"
        }:
            raise InvalidDecision("routine decisions must not be represented as blocked")
        if "needs_human" in json.dumps(blocker).lower():
            raise InvalidDecision("blocked cannot proxy a human decision")


def completion_is_supported(decision: PlannerDecision, steps: list[dict[str, Any]]) -> bool:
    if decision.decision != "complete" or len(decision.evidence) < 2 or not steps:
        return False
    return all(
        step.get("status") == "complete"
        and bool(step.get("resulting_commit"))
        and (step.get("reviewer_feedback") or {}).get("verdict") == "approved"
        for step in steps
    )
