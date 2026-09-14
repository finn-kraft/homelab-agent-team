from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .agent import CoderAgent
from .db import Store
from .llm import OllamaBackend, OpenRouterBackend, Router
from .worker import Worker


def build_agent() -> CoderAgent:
    database_url = os.environ["DATABASE_URL"]
    roots = [p for p in os.environ["CODER_WORKSPACES"].split(os.pathsep) if p]
    local = OllamaBackend(os.getenv("OLLAMA_URL", "http://localhost:11434"),
                          os.getenv("CODER_MODEL", "qwen2.5-coder:14b"))
    cloud = None
    if os.getenv("OPENROUTER_API_KEY"):
        cloud = OpenRouterBackend("https://openrouter.ai/api/v1",
                                  os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
                                  os.environ["OPENROUTER_API_KEY"])
    return CoderAgent(Store(database_url), Router(local, cloud),
                      os.getenv("CODER_WORKER_ID", "coder-1"), roots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coder-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("run")
    once = sub.add_parser("run-once")
    once.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = build_agent()
    if args.command == "init-db":
        agent.store.migrate()
        return 0
    if args.command == "run":
        Worker(agent).run_forever()
        return 0
    task = agent.store.claim(agent.worker_id)
    if not task:
        print(json.dumps({"status": "idle"}) if args.json else "No claimable step.")
        return 0
    result = agent.run_task(task)
    print(json.dumps({"status": result.status, "summary": result.summary,
                      "files_changed": result.files_changed, "blocker": result.blocker},
                     default=str))
    return 0 if result.status.value in {"review", "complete"} else 1


if __name__ == "__main__":
    sys.exit(main())

