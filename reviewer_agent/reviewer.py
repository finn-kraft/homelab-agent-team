from __future__ import annotations

import json

from .decision import InvalidReview, parse_review
from .prompt import SYSTEM_PROMPT


class ReviewerAgent:
    def __init__(
        self,
        store,
        router,
        collector,
        worker_id,
        lease_seconds=300,
        max_attempts=5,
        large_diff_escalates=True,
    ):
        self.store = store
        self.router = router
        self.collector = collector
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.large_diff_escalates = large_diff_escalates

    def review_once(self, step_id=None):
        """Review one item, optionally the exact step claimed by the coordinator."""
        item = self.store.claim(
            self.worker_id,
            self.lease_seconds,
            step_id,
        )

        if not item:
            return None

        files = list(item.get("files_changed") or [])

        evidence = self.collector.collect(
            item["repository"],
            item["starting_commit"],
            files,
            [],
        )

        # Only evidence from this Coder revision is relevant. A prior failed
        # verification must not permanently poison a corrected same-step retry.
        commands = self.store.commands(
            item["id"],
            item["attempt_count"],
        )

        # Failed Coder commands are review evidence, not an automatic rejection.
        # Secret findings and an invalid Git diff remain deterministic gates.
        if (
            evidence["secret_hits"]
            or evidence["checks"]["diff_check"]["exit_code"] != 0
        ):
            reason = (
                "secret detected"
                if evidence["secret_hits"]
                else "git diff validation failed"
            )

            criteria = [
                {
                    "criterion": criterion,
                    "status": "uncertain",
                    "evidence": reason,
                }
                for criterion in item["acceptance_criteria"]
            ]

            payload = {
                "verdict": "changes_requested",
                "summary": reason,
                "blocking_issues": [
                    {
                        "severity": (
                            "critical"
                            if evidence["secret_hits"]
                            else "high"
                        ),
                        "category": (
                            "security"
                            if evidence["secret_hits"]
                            else "testing"
                        ),
                        "problem": reason,
                        "evidence": reason,
                        "requested_change": (
                            "Remove the issue and provide passing verification."
                        ),
                    }
                ],
                "non_blocking_suggestions": [],
                "acceptance_criteria": criteria,
                "risk": "high",
                "confidence": "high",
                "recommended_next_state": "coder_revision",
            }

            decision = parse_review(
                json.dumps(payload),
                item["acceptance_criteria"],
            )

            meta = {
                "model": "deterministic-gate",
                "provider": "local",
                "latency": 0,
                "usage": {},
            }

        elif item["review_attempt"] > self.max_attempts:
            payload = {
                "verdict": "needs_human",
                "summary": "Review loop limit reached",
                "blocking_issues": [],
                "non_blocking_suggestions": [],
                "acceptance_criteria": [
                    {
                        "criterion": criterion,
                        "status": "uncertain",
                        "evidence": "review loop exhausted",
                    }
                    for criterion in item["acceptance_criteria"]
                ],
                "risk": "high",
                "confidence": "high",
                "recommended_next_state": "blocked",
                "human_question": (
                    "The Coder/Reviewer loop reached its configured limit. "
                    "Should this step be re-scoped, waived, or stopped?"
                ),
            }

            decision = parse_review(
                json.dumps(payload),
                item["acceptance_criteria"],
            )

            meta = {
                "model": "loop-policy",
                "provider": "local",
                "latency": 0,
                "usage": {},
            }

        else:
            prior = self.store.prior_issues(item["id"])

            context = {
                "task": {
                    key: item.get(key)
                    for key in (
                        "title",
                        "objective",
                        "acceptance_criteria",
                        "constraints",
                    )
                },
                "implementation": {
                    "files_changed": files,
                    "command_results": commands,
                },
                "evidence": evidence,
                "prior_issues": prior,
            }

            escalate = (
                item["review_attempt"] >= 3
                or evidence["risk_flags"]["large_diff"]
                or evidence["risk_flags"]["migration_change"]
                or evidence["risk_flags"]["financial_change"]
            )

            backend = self.router.choose(
                item["review_attempt"],
                escalate,
            )

            response = backend.complete(
                [
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(context, default=str),
                    },
                ]
            )

            try:
                decision = parse_review(
                    response.text,
                    item["acceptance_criteria"],
                )
            except InvalidReview:
                payload = {
                    "verdict": "blocked",
                    "summary": "invalid model output",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                    "acceptance_criteria": [
                        {
                            "criterion": criterion,
                            "status": "uncertain",
                            "evidence": "review output invalid",
                        }
                        for criterion in item["acceptance_criteria"]
                    ],
                    "risk": "medium",
                    "confidence": "low",
                    "recommended_next_state": "blocked",
                }

                decision = parse_review(
                    json.dumps(payload),
                    item["acceptance_criteria"],
                )

            meta = {
                "model": response.model,
                "provider": response.backend,
                "latency": response.latency_seconds,
                "usage": response.usage,
            }

        self.store.complete(
            item,
            self.worker_id,
            decision,
            meta,
            evidence,
        )

        return decision
