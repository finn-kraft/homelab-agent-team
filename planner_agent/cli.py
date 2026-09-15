from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from agent_core.llm import InferenceRouter, OllamaBackend, OpenRouterBackend
from .inspector import ReadOnlyRepositoryInspector
from .planner import PlannerAgent
from .store import PlannerStore
from .worker import PlannerWorker


def build_planner() -> PlannerAgent:
    store = PlannerStore(os.environ["DATABASE_URL"])
    roots = [value for value in os.environ["PLANNER_ALLOWED_REPOSITORIES"].split(os.pathsep)
             if value]
    local = OllamaBackend(os.getenv("OLLAMA_URL", "http://localhost:11434"),
                          os.getenv("PLANNER_MODEL", "qwen2.5-coder:14b"))
    cloud = None
    if os.getenv("OPENROUTER_API_KEY"):
        cloud = OpenRouterBackend("https://openrouter.ai/api/v1",
                                  os.getenv("OPENROUTER_PLANNER_MODEL",
                                            "anthropic/claude-sonnet-4"),
                                  os.environ["OPENROUTER_API_KEY"])
    escalation_attempt = int(os.getenv(
        "PLANNER_ESCALATE_AFTER", os.getenv("INFERENCE_ESCALATE_AFTER", "4")
    ))
    return PlannerAgent(
        store, InferenceRouter(
            local, cloud,
            router_url=os.getenv("ROUTER_URL", "http://127.0.0.1:8090"),
            caller_agent="planner-agent",
            task_type="planning",
            escalate_after=escalation_attempt,
            timeout=float(os.getenv("ROUTER_TIMEOUT_SECONDS", "3")),
            privacy_sensitive=os.getenv("INFERENCE_PRIVACY_SENSITIVE", "false").lower()
            in {"1", "true", "yes", "on"},
        ),
        ReadOnlyRepositoryInspector(roots),
        os.getenv("PLANNER_WORKER_ID", "planner-1"),
        int(os.getenv("PLANNER_LEASE_SECONDS", "300")),
        int(os.getenv("PLANNER_DECISION_RETRIES", "2")),
        escalation_attempt,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planner-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("run")
    sub.add_parser("once")
    create = sub.add_parser("create-job")
    create.add_argument("goal")
    create.add_argument("--repository", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--priority", type=int, default=0)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("job_id", type=int)
    for command in ("pause", "resume", "cancel", "events"):
        item = sub.add_parser(command)
        item.add_argument("job_id", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    planner = build_planner()
    store = planner.store
    if args.command == "init-db":
        store.migrate()
    elif args.command == "run":
        PlannerWorker(planner).run_forever()
    elif args.command == "once":
        decision = planner.plan_once()
        print(json.dumps(decision.as_dict() if decision else {"decision": "idle"}, default=str))
    elif args.command == "create-job":
        print(store.create_job(args.goal, args.repository, args.branch, args.priority))
    elif args.command in {"inspect", "events"}:
        job, steps, events = store.context(args.job_id)
        value = events if args.command == "events" else {
            "job": PlannerAgent._serializable(job), "steps": steps, "events": events[:10]}
        print(json.dumps(value, default=str, indent=2))
    else:
        getattr(store, args.command)(args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
