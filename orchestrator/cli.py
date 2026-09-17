from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from typing import Sequence

from coder_agent.cli import build_agent
from planner_agent.cli import build_planner
from planner_agent.store import PlannerStore
from reviewer_agent.cli import build as build_reviewer

from .checkpoint import CheckpointService
from .config import OrchestratorConfig
from .orchestrator import AgentOrchestrator
from .store import OrchestratorStore
from .verification import VerificationService


def build_orchestrator(config: OrchestratorConfig | None = None) -> AgentOrchestrator:
    """Construct bounded specialist agents around one deterministic coordinator."""
    config = config or OrchestratorConfig.from_env()
    coder = build_agent()
    reviewer = build_reviewer()
    coder.max_attempts = config.max_coder_attempts
    reviewer.max_attempts = config.max_review_attempts
    return AgentOrchestrator(
        store=OrchestratorStore(config.database_url),
        planner=build_planner(),
        coder=coder,
        reviewer=reviewer,
        verifier=VerificationService(),
        checkpoint=CheckpointService(),
        config=config,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-orchestrator",
        description="Persistent deterministic workflow controller for the Homelab agent team.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="apply additive workflow schema migrations")
    subcommands.add_parser("run", help="run continuously in the foreground")
    subcommands.add_parser("once", help="advance at most one durable workflow action")
    subcommands.add_parser("status", help="show durable job summaries")
    subcommands.add_parser("audit", help="report read-only workflow invariant violations")
    mission = subcommands.add_parser("create-mission", help="create a durable V2 mission")
    mission.add_argument("goal"); mission.add_argument("--repository", required=True); mission.add_argument("--branch", required=True)
    packages = subcommands.add_parser("packages", help="list durable V2 work packages")
    packages.add_argument("--mission-id", type=int)
    inspect = subcommands.add_parser("inspect", help="show a job, its steps, and recent events")
    inspect.add_argument("job_id", type=int)
    for name in ("pause", "resume", "cancel"):
        item = subcommands.add_parser(name, help=f"{name} one job at a safe boundary")
        item.add_argument("job_id", type=int)
    create = subcommands.add_parser("create-job", help="create a persistent high-level job")
    create.add_argument("goal")
    create.add_argument("--repository", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--max-iterations", type=int, default=100)
    return parser


def _install_stop_handlers(orchestrator: AgentOrchestrator) -> None:
    def stop(signum, _frame) -> None:
        logging.getLogger(__name__).info("shutdown_signal signal=%s", signal.Signals(signum).name)
        orchestrator.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = OrchestratorConfig.from_env()
    except (KeyError, RuntimeError, ValueError) as exc:
        logging.getLogger(__name__).error("fatal_startup_configuration_error error=%s", exc)
        return 2

    store = OrchestratorStore(config.database_url)
    orchestrator: AgentOrchestrator | None = None
    try:
        if args.command == "init-db":
            store.migrate()
            print(json.dumps({"status": "ok", "migration": "orchestrator-0003"}))
            return 0
        if args.command == "run":
            orchestrator = build_orchestrator(config)
            _install_stop_handlers(orchestrator)
            orchestrator.run()
            return 0
        if args.command == "once":
            orchestrator = build_orchestrator(config)
            result = orchestrator.once()
            print(json.dumps({
                "action": result.action, "job_id": result.job_id,
                "step_id": result.step_id, "detail": result.detail,
            }))
            return 0
        if args.command == "status":
            print(json.dumps(store.status(), default=str, indent=2))
            return 0
        if args.command == "audit":
            print(json.dumps({"violations": store.invariant_report()}, default=str, indent=2))
            return 0
        if args.command == "create-mission":
            print(json.dumps({"mission_id": store.create_mission(args.goal, args.repository, args.branch)}))
            return 0
        if args.command == "packages":
            print(json.dumps(store.list_work_packages(args.mission_id), default=str, indent=2))
            return 0
        if args.command == "inspect":
            print(json.dumps(store.inspect(args.job_id), default=str, indent=2))
            return 0
        if args.command in {"pause", "resume", "cancel"}:
            store.control(args.job_id, args.command)
            print(json.dumps({"status": "ok", "action": args.command, "job_id": args.job_id}))
            return 0
        if args.command == "create-job":
            if args.max_iterations <= 0:
                raise ValueError("--max-iterations must be greater than zero")
            job_id = PlannerStore(config.database_url).create_job(
                args.goal, args.repository, args.branch, args.priority, args.max_iterations
            )
            print(json.dumps({"job_id": job_id}))
            return 0
    except KeyError as exc:
        logging.getLogger(__name__).error("not_found error=%s", exc)
        return 3
    except Exception as exc:
        logging.getLogger(__name__).exception("command_failed command=%s error=%s", args.command, exc)
        return 1
    return 1  # pragma: no cover - argparse exhausts known commands


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
