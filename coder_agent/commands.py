from __future__ import annotations

import os
import importlib.util
import signal
import shutil
import subprocess
import sys
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
        ("git", "rebase"), ("git", "checkout"), ("git", "switch"),
        # Staging and committing belong only to the deterministic checkpoint
        # service, after independent review and final verification.
        ("git", "add"), ("git", "commit"), ("git", "merge"),
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

    def run(self, argv: list[str], timeout: int = 300, cancel_check=None) -> CommandResult:
        self.policy.validate(argv)
        env = {k: v for k, v in os.environ.items() if k not in {
            "DATABASE_URL", "ALIGN_DATABASE_URL", "OPENROUTER_API_KEY", "GITHUB_TOKEN",
            "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "PGPASSWORD",
        }}
        # Systemd often launches the editable entry point with a minimal PATH.
        # Put the interpreter's virtualenv first so pytest/ruff/etc. resolve to
        # the same environment that runs the EngineeringAgent.
        interpreter_bin = os.path.dirname(sys.executable)
        env["PATH"] = os.pathsep.join(
            part for part in (interpreter_bin, env.get("PATH", "")) if part
        )
        execution_argv = self._resolve_python_tool(argv, env)
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen(
                execution_argv, cwd=self.workspace.root, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=(os.name == "posix"),
            )
            deadline = started + max(1, int(timeout))
            while True:
                if cancel_check is not None:
                    try:
                        cancelled = bool(cancel_check())
                    except Exception:
                        # A transient database read must not turn a safe
                        # command into an untracked crash.
                        cancelled = False
                    if cancelled:
                        stdout, stderr = self._terminate(process)
                        return CommandResult(
                            argv, stdout, stderr or "command cancelled at a safe boundary",
                            130, time.monotonic() - started, False, True,
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = self._terminate(process)
                    return CommandResult(
                        argv, stdout, stderr or "command timed out", 124,
                        time.monotonic() - started, True,
                    )
                try:
                    stdout, stderr = process.communicate(timeout=min(1.0, remaining))
                    return CommandResult(argv, stdout or "", stderr or "", process.returncode,
                                         time.monotonic() - started)
                except subprocess.TimeoutExpired:
                    continue
        except FileNotFoundError:
            command = argv[0] if argv else "command"
            return CommandResult(
                argv,
                "",
                f"{command} is not installed in the agent environment; "
                "install the repository test tools in the service virtualenv "
                "(for example: .venv/bin/python -m pip install -e '.[test]')",
                127,
                time.monotonic() - started,
            )

    @staticmethod
    def _terminate(process: subprocess.Popen) -> tuple[str, str]:
        """Stop a command and its descendants, returning captured output."""
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:  # pragma: no cover - Windows compatibility
                    process.terminate()
                stdout, stderr = process.communicate(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:  # pragma: no cover - Windows compatibility
                        process.kill()
                stdout, stderr = process.communicate()
        else:
            stdout, stderr = process.communicate()
        return stdout or "", stderr or ""

    @staticmethod
    def _resolve_python_tool(argv: list[str], env: dict[str, str]) -> list[str]:
        """Run Python-based tools through the service interpreter when needed."""
        if not argv:
            return argv
        command = argv[0]
        modules = {"pytest": "pytest", "ruff": "ruff", "mypy": "mypy"}
        # Prefer the module attached to the service interpreter even when a
        # different global executable happens to be earlier on PATH. A
        # systemd service must not silently run another environment's pytest.
        if command in modules and importlib.util.find_spec(modules[command]) is not None:
            return [sys.executable, "-m", modules[command], *argv[1:]]
        if command in {"python", "python3"} and shutil.which(command, path=env.get("PATH")) is None:
            return [sys.executable, *argv[1:]]
        return argv
