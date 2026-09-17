from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager

from .decision import InvalidReview, parse_review
from .prompt import SYSTEM_PROMPT
from agent_core.prompt_budget import bounded_messages, bounded_text, message_chars


LOG = logging.getLogger(__name__)


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
        max_context_chars=120_000,
    ):
        self.store = store
        self.router = router
        self.collector = collector
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.large_diff_escalates = large_diff_escalates
        self.max_context_chars = max(16_000, int(max_context_chars))
        self.last_prompt_chars = 0

    def review_once(self, step_id=None):
        """Review one item, optionally the exact step claimed by the coordinator."""
        self.last_prompt_chars = 0
        item = self.store.claim(
            self.worker_id,
            self.lease_seconds,
            step_id,
        )

        if not item:
            return None

        with self._lease_heartbeat(item):
            return self._review_claimed(item)

    def abandon(self, step_id, detail, retry_seconds=20):
        """Release the active review after an exception outside review logic."""
        abandon = getattr(self.store, "abandon", None)
        if abandon is None:
            return False
        return abandon(step_id, self.worker_id, detail, retry_seconds)

    @contextmanager
    def _lease_heartbeat(self, item):
        """Renew both reviewer and step leases while evidence/model work runs."""
        heartbeat = getattr(self.store, "heartbeat", None)
        if heartbeat is None:
            yield
            return
        stop = threading.Event()
        interval = max(1.0, self.lease_seconds / 3)

        def renew() -> None:
            while not stop.wait(interval):
                try:
                    if not heartbeat(item["review_id"], self.worker_id, self.lease_seconds):
                        LOG.error("review_lease_lost review_id=%s step_id=%s",
                                  item.get("review_id"), item.get("id"))
                        return
                except Exception:
                    LOG.exception("review_lease_heartbeat_failed review_id=%s step_id=%s",
                                  item.get("review_id"), item.get("id"))

        thread = threading.Thread(
            target=renew,
            name=f"reviewer-lease-{item['id']}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=min(interval, 2.0))

    def _review_claimed(self, item):
        """Perform the read-only review after the durable claim is recorded."""

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
                "verdict": "blocked",
                "summary": "Review loop exhausted; return for autonomous replanning",
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

            request_messages = bounded_messages([
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": self._context_json(context),
                    },
                ], self.max_context_chars)
            self.last_prompt_chars = message_chars(request_messages)
            response = backend.complete(request_messages)

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

    def _context_json(self, context):
        """Keep reviewer prompts bounded without discarding stored evidence."""
        bounded = dict(context)
        implementation = dict(bounded.get("implementation") or {})
        implementation["command_results"] = [
            self._bounded_value(result, 8_000)
            for result in list(implementation.get("command_results") or [])[-20:]
        ]
        bounded["implementation"] = implementation

        evidence = dict(bounded.get("evidence") or {})
        evidence["diff"] = self._bounded_text(evidence.get("diff", ""), 55_000)
        evidence["documents"] = {
            str(name): self._bounded_text(value, 8_000)
            for name, value in dict(evidence.get("documents") or {}).items()
        }
        evidence["checks"] = self._bounded_value(evidence.get("checks", {}), 8_000)
        evidence["risk_flags"] = self._bounded_value(evidence.get("risk_flags", {}), 2_000)
        bounded["evidence"] = evidence
        bounded["prior_issues"] = [
            self._bounded_value(issue, 4_000)
            for issue in list(bounded.get("prior_issues") or [])[-20:]
        ]
        encoded = json.dumps(bounded, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        # Preserve a valid JSON request even for unusually large metadata.
        compact = {
            "task": bounded.get("task", {}),
            "implementation": {"files_changed": implementation.get("files_changed", [])},
            "evidence": {
                "diff": self._bounded_text(
                    evidence.get("diff", ""), max(1_000, self.max_context_chars // 4)
                ),
                "risk_flags": evidence.get("risk_flags", {}),
                "checks": evidence.get("checks", {}),
            },
            "prior_issues": bounded.get("prior_issues", [])[-5:],
            "context_notice": "Large review evidence was truncated; rely on deterministic checks and the diff summary.",
        }
        encoded = json.dumps(compact, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        # Keep the transport payload valid JSON even when a caller configures
        # an unusually small budget or supplies pathological metadata.
        return json.dumps({
            "task": {
                "objective": self._bounded_text(
                    bounded.get("task", {}).get("objective", ""), 1_000
                )
            },
            "implementation": {
                "files_changed": [
                    self._bounded_text(value, 200)
                    for value in implementation.get("files_changed", [])[:20]
                ]
            },
            "context_notice": "Review evidence was truncated to the configured model budget.",
        }, default=str)

    @staticmethod
    def _bounded_text(value, limit):
        return bounded_text(value, limit)

    @classmethod
    def _bounded_value(cls, value, value_limit, depth=0):
        if isinstance(value, (str, bytes)):
            return cls._bounded_text(value, value_limit)
        if depth >= 4:
            return cls._bounded_text(value, value_limit)
        if isinstance(value, dict):
            return {
                str(key): cls._bounded_value(item, value_limit, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._bounded_value(item, value_limit, depth + 1) for item in list(value)[:20]]
        return value
