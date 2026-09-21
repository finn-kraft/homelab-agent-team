from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from typing import Sequence

from engineering_agent.cli import build_engineer
from planner_agent.cli import build_planner
from planner_agent.store import PlannerStore
from reviewer_agent.cli import build as build_reviewer

from .checkpoint import CheckpointService
from .config import OrchestratorConfig
from .integration import IntegrationManager
from .mission import MissionManager
from .orchestrator import AgentOrchestrator
from .store import MIGRATION_VERSION, OrchestratorStore
from .verification import VerificationService


def build_orchestrator(config: OrchestratorConfig | None = None) -> AgentOrchestrator:
    """Construct bounded specialist agents around one deterministic coordinator."""
    config = config or OrchestratorConfig.from_env()
    engineer = build_engineer()
    reviewer = build_reviewer()
    engineer.max_attempts = config.engineering_attempt_limit
    reviewer.max_attempts = config.max_review_attempts
    return AgentOrchestrator(
        store=OrchestratorStore(config.database_url),
        planner=build_planner(),
        engineer=engineer,
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
    expand = subcommands.add_parser("expand-mission", help="materialize unchecked roadmap items as packages")
    expand.add_argument("mission_id", type=int); expand.add_argument("--roadmap", default="docs/roadmap.md"); expand.add_argument("--limit", type=int, default=3)
    subcommands.add_parser("missions", help="list durable V2 missions")
    packages = subcommands.add_parser("packages", help="list durable V2 work packages")
    packages.add_argument("--mission-id", type=int)
    package = subcommands.add_parser("create-package", help="create a V2 package and linked V1 step")
    package.add_argument("objective"); package.add_argument("--mission-id", type=int, required=True)
    package.add_argument("--repository", required=True); package.add_argument("--branch", required=True)
    package.add_argument("--acceptance", action="append", required=True)
    claim = subcommands.add_parser("claim-package", help="claim one ready V2 package")
    claim.add_argument("--worker-id", default="engineering-1"); claim.add_argument("--lease-seconds", type=int, default=900)
    subcommands.add_parser("recover-packages", help="return expired V2 package leases to ready")
    queue = subcommands.add_parser("human-queue", help="list durable human decisions")
    queue.add_argument(
        "--status", default="open",
        choices=("open", "answered", "cancelled", "resolved", "all"),
    )
    answer = subcommands.add_parser("answer-human", help="answer a durable human decision")
    answer.add_argument("request_id", type=int); answer.add_argument("answer")
    integrate = subcommands.add_parser("integrate-package", help="merge a verified package into its mission branch")
    integrate.add_argument("package_id", type=int); integrate.add_argument("--repository", required=True); integrate.add_argument("--target-branch")
    advance = subcommands.add_parser("advance-package", help="advance a leased V2 package lifecycle state")
    advance.add_argument("package_id", type=int); advance.add_argument("current"); advance.add_argument("next_status")
    advance.add_argument("--worker-id", default="engineering-1"); advance.add_argument("--commit")
    inspect = subcommands.add_parser("inspect", help="show a job, its steps, and recent events")
    inspect.add_argument("job_id", type=int)
    for name in ("pause", "resume", "cancel"):
        item = subcommands.add_parser(name, help=f"{name} one job at a safe boundary")
        item.add_argument("job_id", type=int)
    retry = subcommands.add_parser("retry", help="requeue failed or exhausted work")
    retry.add_argument("job_id", type=int)
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
            print(json.dumps({"status": "ok", "migration": MIGRATION_VERSION}))
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
        if args.command == "missions":
            print(json.dumps(store.list_missions(), default=str, indent=2))
            return 0
        if args.command == "expand-mission":
            ids = MissionManager(store, max_packages=args.limit).ensure_packages(
                args.mission_id, roadmap=args.roadmap, limit=args.limit
            )
            print(json.dumps({"created_package_ids": ids}))
            return 0
        if args.command == "packages":
            print(json.dumps(store.list_work_packages(args.mission_id), default=str, indent=2))
            return 0
        if args.command == "create-package":
            package_id = store.create_work_package(args.mission_id, args.objective, args.repository,
                args.branch, args.acceptance)
            print(json.dumps({"package_id": package_id}))
            return 0
        if args.command == "claim-package":
            print(json.dumps(store.claim_work_package(args.worker_id, args.lease_seconds), default=str))
            return 0
        if args.command == "recover-packages":
            print(json.dumps({"recovered": store.recover_work_packages()}))
            return 0
        if args.command == "human-queue":
            print(json.dumps(store.list_human_queue(args.status), default=str, indent=2))
            return 0
        if args.command == "answer-human":
            store.answer_human_request(
                args.request_id, args.answer, answered_by="orchestrator-cli"
            )
            print(json.dumps({"status": "answered", "request_id": args.request_id}))
            return 0
        if args.command == "integrate-package":
            package = next((p for p in store.list_work_packages() if int(p["id"]) == args.package_id), None)
            if not package:
                raise KeyError(args.package_id)
            result = IntegrationManager(store, protected_branches=config.protected_branches).integrate(
                package, args.repository, target_branch=args.target_branch
            )
            print(json.dumps(result, default=str))
            return 0
        if args.command == "advance-package":
            store.advance_work_package(args.package_id, args.worker_id, args.current,
                                       args.next_status, args.commit)
            print(json.dumps({"status": args.next_status, "package_id": args.package_id}))
            return 0
        if args.command == "inspect":
            print(json.dumps(store.inspect(args.job_id), default=str, indent=2))
            return 0
        if args.command in {"pause", "resume", "cancel"}:
            store.control(args.job_id, args.command)
            print(json.dumps({"status": "ok", "action": args.command, "job_id": args.job_id}))
            return 0
        if args.command == "retry":
            store.retry_job(args.job_id)
            print(json.dumps({"status": "ok", "action": "retry", "job_id": args.job_id}))
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
