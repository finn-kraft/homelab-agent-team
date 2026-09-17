from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .agent import EngineeringAgent
from .db import Store
from .llm import (
    InferenceRouter,
    OllamaBackend,
    OpenRouterBackend,
)
from .worker import Worker


OPENROUTER_URL = "https://openrouter.ai/api/v1"


def build_engineer() -> EngineeringAgent:
    database_url = os.environ["DATABASE_URL"]

    workspace_roots = os.getenv("ENGINEERING_WORKSPACES", os.getenv("CODER_WORKSPACES", ""))
    roots = [path for path in workspace_roots.split(os.pathsep) if path]
    if not roots:
        raise KeyError("ENGINEERING_WORKSPACES")

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
            "ENGINEERING_MODEL",
            os.getenv("CODER_MODEL", "qwen2.5-coder:14b"),
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
                "OPENROUTER_ENGINEERING_STANDARD_MODEL",
                os.getenv("OPENROUTER_CODER_STANDARD_MODEL", "qwen/qwen3-coder-next"),
            ),
            api_key,
            timeout=cloud_timeout,
            retries=cloud_retries,
        )

        premium_cloud = OpenRouterBackend(
            OPENROUTER_URL,
            os.getenv(
                "OPENROUTER_ENGINEERING_PREMIUM_MODEL",
                os.getenv("OPENROUTER_CODER_PREMIUM_MODEL", "openai/gpt-5.2-codex"),
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
        caller_agent="engineering-agent",
        task_type="code_implementation",
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

    return EngineeringAgent(
        Store(database_url),
        router,
        os.getenv(
            "ENGINEERING_WORKER_ID",
            os.getenv("CODER_WORKER_ID", "engineering-1"),
        ),
        roots,
        max_turns=int(
            os.getenv(
                "ENGINEERING_MAX_TURNS",
                os.getenv("CODER_MAX_TURNS", "30"),
            )
        ),
        max_attempts=int(
            os.getenv(
                "MAX_ENGINEERING_ATTEMPTS",
                os.getenv("MAX_CODER_ATTEMPTS", "5"),
            )
        ),
        max_prompt_chars=int(os.getenv(
            "ENGINEERING_MAX_CONTEXT_CHARS",
            os.getenv("CODER_MAX_CONTEXT_CHARS", "120000"),
        )),
    )


# Compatibility for existing systemd units and V1 scripts.
build_agent = build_engineer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engineering-agent" if os.path.basename(sys.argv[0]) == "engineering-agent" else "coder-agent"
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    sub.add_parser("init-db")
    sub.add_parser("run")

    once = sub.add_parser("run-once")
    once.add_argument(
        "--json",
        action="store_true",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    agent = build_agent()

    if args.command == "init-db":
        agent.store.migrate()
        return 0

    if args.command == "run":
        Worker(agent).run_forever()
        return 0

    task = agent.store.claim(
        agent.worker_id
    )

    if not task:
        print(
            json.dumps({"status": "idle"})
            if args.json
            else "No claimable step."
        )
        return 0

    result = agent.run_task(task)

    print(
        json.dumps(
            {
                "status": result.status,
                "summary": result.summary,
                "files_changed": result.files_changed,
                "blocker": result.blocker,
            },
            default=str,
        )
    )

    return (
        0
        if result.status.value in {
            "review",
            "complete",
        }
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
