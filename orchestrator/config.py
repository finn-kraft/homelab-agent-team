from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _commands() -> tuple[tuple[str, ...], ...]:
    """Read an optional explicit JSON command list without accepting shell text."""
    raw = os.getenv("ORCHESTRATOR_VERIFICATION_COMMANDS")
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ORCHESTRATOR_VERIFICATION_COMMANDS must be JSON") from exc
    if not isinstance(value, list) or not all(
        isinstance(item, list) and item and all(isinstance(arg, str) for arg in item)
        for item in value
    ):
        raise ValueError(
            "ORCHESTRATOR_VERIFICATION_COMMANDS must be a JSON list of argv lists"
        )
    return tuple(tuple(item) for item in value)


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    database_url: str
    worker_id: str
    poll_seconds: float = 10.0
    lease_seconds: int = 300
    repository_lock_seconds: int = 900
    protected_branches: tuple[str, ...] = ("main", "master")
    auto_commit: bool = True
    auto_push: bool = False
    verification_commands: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    verification_timeout_seconds: int = 900
    max_coder_attempts: int = 5
    max_review_attempts: int = 5
    mission_package_limit: int = 3
    auto_integrate: bool = False

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required for agent-orchestrator")
        poll_seconds = float(os.getenv("ORCHESTRATOR_POLL_SECONDS", "10"))
        if poll_seconds <= 0:
            raise ValueError("ORCHESTRATOR_POLL_SECONDS must be greater than zero")
        worker_default = f"orchestrator-{socket.gethostname()}"
        return cls(
            database_url=database_url,
            worker_id=os.getenv("ORCHESTRATOR_WORKER_ID", worker_default),
            poll_seconds=poll_seconds,
            lease_seconds=_positive_int("ORCHESTRATOR_LEASE_SECONDS", 300),
            repository_lock_seconds=_positive_int("ORCHESTRATOR_REPOSITORY_LOCK_SECONDS", 900),
            protected_branches=_csv("PROTECTED_BRANCHES", ("main", "master")),
            auto_commit=_bool("AUTO_COMMIT", True),
            auto_push=_bool("AUTO_PUSH", False),
            verification_commands=_commands(),
            verification_timeout_seconds=_positive_int(
                "ORCHESTRATOR_VERIFICATION_TIMEOUT_SECONDS", 900
            ),
            max_coder_attempts=_positive_int("MAX_CODER_ATTEMPTS", 5),
            max_review_attempts=_positive_int("MAX_REVIEW_ATTEMPTS", 5),
            mission_package_limit=_positive_int("MISSION_PACKAGE_LIMIT", 3),
            auto_integrate=_bool("AUTO_INTEGRATE", False),
        )
