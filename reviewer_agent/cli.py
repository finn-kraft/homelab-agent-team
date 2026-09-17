from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from agent_core.llm import (
    InferenceRouter,
    OllamaBackend,
    OpenRouterBackend,
)
from planner_agent.store import PlannerStore

from .evidence import EvidenceCollector
from .reviewer import ReviewerAgent
from .store import ReviewerStore
from .worker import ReviewerWorker


OPENROUTER_URL = "https://openrouter.ai/api/v1"


def build() -> ReviewerAgent:
    store = ReviewerStore(
        os.environ["DATABASE_URL"]
    )

    roots = [
        value
        for value in os.environ[
            "REVIEWER_ALLOWED_REPOSITORIES"
        ].split(os.pathsep)
        if value
    ]

    llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    llm_retries = int(os.getenv("LLM_RETRIES", "0"))
    ollama_timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", str(llm_timeout)))
    ollama_retries = int(os.getenv("OLLAMA_RETRIES", str(llm_retries)))
    cloud_timeout = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "90"))
    cloud_retries = int(os.getenv("OPENROUTER_RETRIES", "1"))

    local = OllamaBackend(
        os.getenv(
            "OLLAMA_URL",
            "http://localhost:11434",
        ),
        os.getenv(
            "REVIEWER_MODEL",
            "qwen2.5-coder:14b",
        ),
        timeout=ollama_timeout,
        retries=ollama_retries,
    )

    standard_cloud = None
    premium_cloud = None

    api_key = os.getenv("OPENROUTER_API_KEY")

    if api_key:
        standard_cloud = OpenRouterBackend(
            OPENROUTER_URL,
            os.getenv(
                "OPENROUTER_REVIEWER_STANDARD_MODEL",
                "qwen/qwen3-coder-next",
            ),
            api_key,
            timeout=cloud_timeout,
            retries=cloud_retries,
        )

        premium_cloud = OpenRouterBackend(
            OPENROUTER_URL,
            os.getenv(
                "OPENROUTER_REVIEWER_PREMIUM_MODEL",
                "anthropic/claude-sonnet-4.6",
            ),
            api_key,
            timeout=cloud_timeout,
            retries=cloud_retries,
        )

    local_attempts = int(os.getenv("INFERENCE_LOCAL_ATTEMPTS", "3"))
    escalation_attempt = int(os.getenv(
        "INFERENCE_ESCALATE_AFTER", str(local_attempts + 1)
    ))

    router = InferenceRouter(
        local,
        standard_cloud,
        premium_cloud,
        router_url=os.getenv(
            "ROUTER_URL",
            "http://127.0.0.1:8090",
        ),
        caller_agent="reviewer-agent",
        task_type="code_review",
        escalate_after=escalation_attempt,
        timeout=float(
            os.getenv(
                "ROUTER_TIMEOUT_SECONDS",
                "3",
            )
        ),
        privacy_sensitive=os.getenv(
            "INFERENCE_PRIVACY_SENSITIVE",
            "false",
        ).lower()
        in {"1", "true", "yes", "on"},
    )

    return ReviewerAgent(
        store,
        router,
        EvidenceCollector(
            roots,
            int(
                os.getenv(
                    "REVIEWER_MAX_DIFF_BYTES",
                    "300000",
                )
            ),
            int(os.getenv("REVIEWER_MAX_DOCUMENT_CHARS", "20000")),
            int(os.getenv("REVIEWER_MAX_COMMAND_OUTPUT_CHARS", "50000")),
        ),
        os.getenv(
            "REVIEWER_WORKER_ID",
            "reviewer-1",
        ),
        int(
            os.getenv(
                "REVIEWER_LEASE_SECONDS",
                "300",
            )
        ),
        int(
            os.getenv(
                "REVIEWER_MAX_ATTEMPTS",
                os.getenv(
                    "MAX_REVIEW_ATTEMPTS",
                    "5",
                ),
            )
        ),
        max_context_chars=int(os.getenv("REVIEWER_MAX_CONTEXT_CHARS", "120000")),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviewer-agent"
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    for command in (
        "init-db",
        "run",
        "once",
    ):
        sub.add_parser(command)

    for command in (
        "inspect",
        "inspect-step",
        "issues",
    ):
        item = sub.add_parser(command)
        item.add_argument(
            "id",
            type=int,
        )

    args = parser.parse_args(argv)
    reviewer = build()

    if args.command == "init-db":
        PlannerStore(
            os.environ["DATABASE_URL"]
        ).migrate()

    elif args.command == "run":
        ReviewerWorker(
            reviewer
        ).run_forever()

    elif args.command == "once":
        decision = reviewer.review_once()

        print(
            json.dumps(
                (
                    asdict(decision)
                    if decision
                    else {"verdict": "idle"}
                ),
                default=str,
            )
        )

    else:
        function = {
            "inspect": reviewer.store.inspect_review,
            "inspect-step": reviewer.store.inspect_step,
            "issues": reviewer.store.issues,
        }[args.command]

        print(
            json.dumps(
                function(args.id),
                default=str,
                indent=2,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
