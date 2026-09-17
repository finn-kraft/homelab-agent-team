"""Deterministic verification for an approved implementation candidate.

The verifier deliberately treats project instructions as data, never as shell
scripts.  It only runs explicit argv commands whose executable is allowlisted,
and always performs a Git whitespace check and a local secret scan first.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence


class VerificationError(RuntimeError):
    """Raised when a verification request cannot be safely constructed."""


class VerificationConfigurationError(VerificationError):
    """Raised when an instruction/configuration attempts an unsafe command."""


_DEFAULT_ALLOWED_COMMANDS = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "ruff",
        "mypy",
        "npm",
        "pnpm",
        "yarn",
        "node",
        "go",
        "cargo",
        "make",
        "cmake",
        "gradle",
        "mvn",
        "gitleaks",
        "uv",
        "poetry",
    }
)
_SHELL_TOKENS = ("\n", "\x00", "&&", "||", ";", "|", ">", "<", "`", "$(")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """One configured verification command represented as an argv list."""

    argv: tuple[str, ...]
    source: str


@dataclass(slots=True)
class VerificationCommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    source: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class VerificationResult:
    passed: bool
    repository: str
    starting_commit: str
    files: list[str]
    checks: list[VerificationCommandResult] = field(default_factory=list)
    secret_findings: list[str] = field(default_factory=list)
    failure_summary: str | None = None
    failure_class: str | None = None

    # Compatibility with the persistence boundary: command output is kept in
    # a single canonical list, while these aliases make the result convenient
    # for append-only command/event storage.
    @property
    def commands(self) -> list[VerificationCommandResult]:
        return self.checks

    @property
    def summary(self) -> str:
        return self.failure_summary or "verification passed"

    @property
    def secret_hits(self) -> list[str]:
        return self.secret_findings

    @property
    def diff_check(self) -> dict[str, object] | None:
        for check in self.checks:
            if check.source == "deterministic:git-diff-check":
                return check.as_dict()
        return None

    @property
    def skipped(self) -> bool:
        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "repository": self.repository,
            "starting_commit": self.starting_commit,
            "files": self.files,
            "checks": [check.as_dict() for check in self.checks],
            "secret_findings": self.secret_findings,
            "failure_summary": self.failure_summary,
            "failure_class": self.failure_class,
        }


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    """Safety and execution settings for :class:`VerificationService`."""

    timeout_seconds: int = 900
    max_output_bytes: int = 100_000
    max_file_scan_bytes: int = 1_000_000
    allowed_commands: frozenset[str] = _DEFAULT_ALLOWED_COMMANDS


class SecretScanner:
    """Small deterministic scanner used as a commit gate, not a DLP product."""

    _patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("private_key", re.compile(r"-----BEGIN [^-\n]{0,80}PRIVATE KEY-----")),
        ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
        ("openai_or_openrouter_key", re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{16,}\b")),
        ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        (
            "database_url_with_password",
            re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb)\+?[A-Za-z0-9_-]*://[^\s:/]+:[^@\s]+@", re.I),
        ),
        (
            "generic_secret_assignment",
            re.compile(
                r"\b(?:database_url|openrouter_api_key|api[_-]?key|secret|password)\s*=\s*['\"][^'\"\s]{8,}['\"]",
                re.I,
            ),
        ),
    )

    def scan_text(self, text: str) -> list[str]:
        return [name for name, pattern in self._patterns if pattern.search(text)]

    def redact(self, text: str) -> str:
        redacted = text
        for name, pattern in self._patterns:
            redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
        return redacted


class VerificationService:
    """Run deterministic, project-authorized validation without a shell."""

    instruction_files = ("WORKER.md", "AGENTS.md")

    def __init__(
        self,
        config: VerificationConfig | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> None:
        self.config = config or VerificationConfig()
        self.secret_scanner = secret_scanner or SecretScanner()

    def verify(
        self,
        repository: str | Path,
        starting_commit: str,
        approved_files: Sequence[str],
        commands: Sequence[Sequence[str]] | None = None,
        *,
        config_commands: Sequence[Sequence[str]] | None = None,
        timeout_seconds: int | None = None,
    ) -> VerificationResult:
        """Verify only ``approved_files`` against an immutable starting commit.

        ``commands`` and ``config_commands`` are argv sequences, never shell
        strings.  If neither is supplied, commands are discovered from the
        explicit ``## Verification`` sections of ``WORKER.md`` then
        ``AGENTS.md``.  A config list deliberately overrides documentation so
        an operator can pin a known-safe CI command set.
        """
        if commands is not None and config_commands is not None:
            raise VerificationConfigurationError(
                "provide either commands or config_commands, not both"
            )
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise VerificationConfigurationError("timeout_seconds must be greater than zero")
            if timeout_seconds != self.config.timeout_seconds:
                # Do not mutate a shared service configuration: an
                # orchestrator may run several read-only checks concurrently
                # in a future worktree-aware deployment.
                return VerificationService(
                    replace(self.config, timeout_seconds=timeout_seconds), self.secret_scanner
                ).verify(
                    repository,
                    starting_commit,
                    approved_files,
                    commands,
                    config_commands=config_commands,
                )
        root = self._repository_root(repository)
        if not _COMMIT_RE.fullmatch(starting_commit):
            raise VerificationError("starting_commit must be a Git commit SHA")
        files = self._safe_files(root, approved_files)
        if not files:
            raise VerificationError("verification requires at least one approved file")

        supplied = commands if commands is not None else config_commands
        if supplied is None:
            # Read project verification policy from the reviewed baseline,
            # rather than a candidate that could modify WORKER.md and add a
            # command to its own verification gate.
            selected = self.discover_commands(root, starting_commit=starting_commit)
        else:
            selected = self._configured_commands(supplied, "configuration")

        checks: list[VerificationCommandResult] = []
        checks.append(
            self._run(
                root,
                ("git", "diff", "--check", starting_commit, "--", *files),
                source="deterministic:git-diff-check",
            )
        )
        for command in selected:
            checks.append(self._run(root, command.argv, source=command.source))

        secret_findings = self._scan_candidate(root, starting_commit, files)
        failed_checks = [check for check in checks if not check.passed]
        summary: str | None = None
        if secret_findings:
            summary = "secret scan found: " + ", ".join(sorted(set(secret_findings)))
        elif failed_checks:
            summary = self._failure_summary(failed_checks)

        failure_class = None
        if secret_findings:
            failure_class = "SECRET"
        elif failed_checks:
            first = failed_checks[0]
            failure_class = "TIMEOUT" if first.timed_out else (
                "ENVIRONMENT_FAILURE" if first.exit_code == 127 else "TEST_FAILURE"
            )
        return VerificationResult(
            passed=not failed_checks and not secret_findings,
            repository=str(root),
            starting_commit=starting_commit,
            files=files,
            checks=checks,
            secret_findings=sorted(set(secret_findings)),
            failure_summary=summary,
            failure_class=failure_class,
        )

    def discover_commands(
        self, repository: str | Path, *, starting_commit: str | None = None
    ) -> list[VerificationCommand]:
        """Read explicit verification commands from trusted project instructions.

        When ``starting_commit`` is supplied, instructions are read from that
        committed baseline.  This prevents the candidate under review from
        changing ``WORKER.md`` and making the verifier execute a new command.
        """
        root = self._repository_root(repository)
        if starting_commit is not None and not _COMMIT_RE.fullmatch(starting_commit):
            raise VerificationError("starting_commit must be a Git commit SHA")
        discovered: list[VerificationCommand] = []
        seen: set[tuple[str, ...]] = set()
        for name in self.instruction_files:
            if starting_commit is None:
                path = root / name
                if not path.is_file():
                    continue
                text = path.read_text(errors="replace")
            else:
                baseline = self._run(
                    root,
                    ("git", "show", f"{starting_commit}:{name}"),
                    source=f"deterministic:instruction-baseline:{name}",
                    # This text is transient policy input, not persisted in a
                    # command result.  Redacting it before parsing could turn
                    # a malformed instruction into a different command.
                    redact_output=False,
                )
                if not baseline.passed:
                    # The file simply did not exist at the baseline; do not
                    # invent a replacement command from the candidate.
                    continue
                text = baseline.stdout
            section = self._verification_section(text)
            for quoted in re.findall(r"`([^`]+)`", section):
                try:
                    argv = tuple(shlex.split(quoted, posix=True))
                except ValueError as exc:
                    raise VerificationConfigurationError(
                        f"cannot parse verification command in {name}: {exc}"
                    ) from exc
                command = self._validate_command(argv, f"{name}:verification")
                if command.argv not in seen:
                    discovered.append(command)
                    seen.add(command.argv)
        return discovered

    def _configured_commands(
        self, commands: Sequence[Sequence[str]], source: str
    ) -> list[VerificationCommand]:
        result: list[VerificationCommand] = []
        for argv in commands:
            result.append(self._validate_command(tuple(argv), source))
        return result

    @staticmethod
    def _verification_section(text: str) -> str:
        match = re.search(r"(?im)^##\s+verification\s*$", text)
        if not match:
            return ""
        after = text[match.end() :]
        next_heading = re.search(r"(?im)^##\s+", after)
        return after[: next_heading.start()] if next_heading else after

    def _validate_command(self, argv: tuple[str, ...], source: str) -> VerificationCommand:
        if not argv:
            raise VerificationConfigurationError(f"empty verification command from {source}")
        if argv[0] not in self.config.allowed_commands:
            raise VerificationConfigurationError(
                f"verification command {argv[0]!r} from {source} is not allowlisted"
            )
        if any(any(token in arg for token in _SHELL_TOKENS) for arg in argv):
            raise VerificationConfigurationError(
                f"verification command from {source} contains shell syntax"
            )
        return VerificationCommand(argv=argv, source=source)

    def _repository_root(self, repository: str | Path) -> Path:
        root = Path(repository).expanduser().resolve(strict=True)
        if not root.is_dir() or not (root / ".git").exists():
            raise VerificationError("repository must be an existing Git working tree")
        return root

    @staticmethod
    def _safe_files(root: Path, values: Sequence[str]) -> list[str]:
        files: list[str] = []
        seen: set[str] = set()
        for value in values:
            path = Path(value)
            if not value or path.is_absolute() or "\x00" in value:
                raise VerificationError("approved file path must be relative")
            resolved = (root / path).resolve(strict=False)
            if resolved == root or not resolved.is_relative_to(root):
                raise VerificationError(f"approved file path escapes repository: {value!r}")
            relative = resolved.relative_to(root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                raise VerificationError(".git paths cannot be approved files")
            if relative not in seen:
                files.append(relative)
                seen.add(relative)
        return files

    def _run(
        self,
        root: Path,
        argv: Iterable[str],
        *,
        source: str,
        redact_output: bool = True,
    ) -> VerificationCommandResult:
        argv_list = list(argv)
        execution_argv = argv_list
        if argv_list and argv_list[0] == "python" and shutil.which("python") is None:
            execution_argv = [sys.executable, *argv_list[1:]]
        started = time.monotonic()
        try:
            done = subprocess.run(
                execution_argv,
                cwd=root,
                env=self._safe_environment(),
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
                shell=False,
            )
            stdout, stderr, exit_code, timed_out = (
                done.stdout,
                done.stderr,
                done.returncode,
                False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._as_text(exc.stdout)
            stderr = self._as_text(exc.stderr)
            exit_code, timed_out = 124, True
        except OSError as exc:
            stdout, stderr, exit_code, timed_out = "", str(exc), 127, False
        return VerificationCommandResult(
            argv=argv_list,
            exit_code=exit_code,
            stdout=self._truncate_and_redact(stdout) if redact_output else stdout,
            stderr=self._truncate_and_redact(stderr) if redact_output else stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            source=source,
        )

    def _scan_candidate(self, root: Path, starting_commit: str, files: list[str]) -> list[str]:
        # Diff covers tracked changes; reading the candidate files additionally
        # covers new/untracked files that Git diff does not include by default.
        diff = self._run(
            root,
            ("git", "diff", "--no-ext-diff", starting_commit, "--", *files),
            source="deterministic:secret-diff",
            # This transient result is never persisted or returned.  Scanning
            # the original diff is necessary; a redacted diff cannot prove a
            # secret was present.
            redact_output=False,
        )
        findings = self.secret_scanner.scan_text(diff.stdout)
        for relative in files:
            path = root / relative
            if not path.is_file():  # deletion is represented safely in the diff
                continue
            try:
                contents = path.read_bytes()[: self.config.max_file_scan_bytes]
            except OSError:
                continue
            findings.extend(self.secret_scanner.scan_text(contents.decode("utf-8", "replace")))
        return findings

    def _truncate_and_redact(self, value: str) -> str:
        redacted = self.secret_scanner.redact(value)
        encoded = redacted.encode("utf-8", "replace")
        if len(encoded) <= self.config.max_output_bytes:
            return redacted
        truncated = encoded[: self.config.max_output_bytes].decode("utf-8", "ignore")
        return truncated + "\n[TRUNCATED]"

    @staticmethod
    def _as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        blocked = {
            "DATABASE_URL",
            "ALIGN_DATABASE_URL",
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key not in blocked
            and not key.endswith("_PASSWORD")
            and not key.endswith("_TOKEN")
            and not key.endswith("_SECRET")
        }

    @staticmethod
    def _failure_summary(checks: list[VerificationCommandResult]) -> str:
        first = checks[0]
        suffix = " timed out" if first.timed_out else f" exited {first.exit_code}"
        return f"verification command {' '.join(first.argv)}{suffix}"
