from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

from .models import CommandResult
from .workspace import Workspace


class CommandRejected(ValueError):
    pass


@dataclass(slots=True)
class CommandPolicy:
    allowed: set[str] = field(default_factory=lambda: {
        "git", "pytest", "python", "python3", "ruff", "mypy", "npm", "pnpm",
        "yarn", "node", "go", "cargo", "make", "cmake", "gradle", "mvn",
    })
    denied_subcommands: set[tuple[str, str]] = field(default_factory=lambda: {
        ("git", "push"), ("git", "reset"), ("git", "clean"),
        ("git", "rebase"), ("git", "checkout"),
    })

    def validate(self, argv: list[str]) -> None:
        if not argv or argv[0] not in self.allowed:
            raise CommandRejected("command is not on the allowlist")
        if len(argv) > 1 and (argv[0], argv[1]) in self.denied_subcommands:
            raise CommandRejected("command requires a separately approved workflow")
        if any("\n" in arg or "\x00" in arg for arg in argv):
            raise CommandRejected("invalid command argument")


class CommandRunner:
    def __init__(self, workspace: Workspace, policy: CommandPolicy | None = None):
        self.workspace = workspace
        self.policy = policy or CommandPolicy()

    def run(self, argv: list[str], timeout: int = 300) -> CommandResult:
        self.policy.validate(argv)
        env = {k: v for k, v in os.environ.items() if k not in {
            "OPENROUTER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY"
        }}
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv, cwd=self.workspace.root, env=env, text=True,
                capture_output=True, timeout=timeout, shell=False,
            )
            return CommandResult(argv, done.stdout, done.stderr, done.returncode,
                                 time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(argv, exc.stdout or "", exc.stderr or "", 124,
                                 time.monotonic() - started, True)

