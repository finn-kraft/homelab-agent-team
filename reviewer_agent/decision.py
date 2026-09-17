from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


VERDICTS = {"approved", "changes_requested", "blocked"}
SEVERITIES = {"critical", "high", "medium", "low", "info"}
CRITERION_STATUSES = {"satisfied", "not_satisfied", "uncertain", "not_applicable"}


class InvalidReview(ValueError): pass


@dataclass(slots=True)
class ReviewDecision:
    verdict: str
    summary: str
    blocking_issues: list[dict[str, Any]]
    non_blocking_suggestions: list[dict[str, Any]]
    acceptance_criteria: list[dict[str, str]]
    risk: str
    recommended_next_state: str
    confidence: str = "medium"
    human_question: str | None = None


def stable_issue_key(issue: dict[str, Any]) -> str:
    basis = "|".join(str(issue.get(k, "")).lower().strip()
                     for k in ("category", "file", "problem"))
    return "REV-" + hashlib.sha256(basis.encode()).hexdigest()[:8].upper()


def parse_review(text: str, criteria: list[str]) -> ReviewDecision:
    try: data = json.loads(text)
    except json.JSONDecodeError as exc: raise InvalidReview("invalid JSON") from exc
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        raise InvalidReview("unknown verdict")
    matrix = data.get("acceptance_criteria", [])
    if len(matrix) != len(criteria): raise InvalidReview("every criterion must be evaluated")
    by_name = {item.get("criterion"): item for item in matrix if isinstance(item, dict)}
    if set(by_name) != set(criteria): raise InvalidReview("criterion matrix does not match task")
    if any(item.get("status") not in CRITERION_STATUSES or not item.get("evidence")
           for item in matrix): raise InvalidReview("criterion status requires evidence")
    issues = data.get("blocking_issues", [])
    for issue in issues:
        if issue.get("severity") not in SEVERITIES: raise InvalidReview("invalid severity")
        for key in ("category", "problem", "evidence", "requested_change"):
            if not issue.get(key): raise InvalidReview("blocking issue is not actionable")
        issue.setdefault("id", stable_issue_key(issue))
    verdict = data["verdict"]
    if verdict == "approved" and (issues or any(i["status"] in {"not_satisfied", "uncertain"} for i in matrix)):
        raise InvalidReview("approval conflicts with blocking evidence")
    if verdict == "changes_requested" and not issues: raise InvalidReview("rejection needs blocking issues")
    if verdict == "needs_human" and not data.get("human_question"):
        raise InvalidReview("human review needs a specific question")
    expected = {"verification"} if verdict == "approved" else {
        "coder_revision", "engineering_revision"
    }
    next_state = data.get("recommended_next_state")
    if verdict in {"approved", "changes_requested"} and next_state not in expected:
        raise InvalidReview("unsafe next state")
    if verdict == "changes_requested" and next_state == "engineering_revision":
        next_state = "coder_revision"  # durable V1 value retained for compatibility
    return ReviewDecision(verdict, str(data.get("summary", "")), issues,
                          list(data.get("non_blocking_suggestions", [])), matrix,
                          str(data.get("risk", "medium")), next_state or "blocked",
                          str(data.get("confidence", "medium")), data.get("human_question"))
