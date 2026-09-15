"""Controlled Git checkpoints for reviewer-approved work.

This module is intentionally separate from the Coder.  It never infers a
file list, never stages a repository wholesale, never commits a protected
branch, and never pushes unless an operator explicitly enables that policy.
The caller supplies the reviewed file list and any pre-existing human changes
captured before the Coder started.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .verification import SecretScanner, VerificationService


class CheckpointError(RuntimeError):
    """A checkpoint could not safely be created."""


_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/=-]{2,200}$")


@dataclass(frozen=True, slots=True)
class CheckpointConfig:
    protected_branches: tuple[str, ...] = ("main", "master")
    timeout_seconds: int = 180
    max_output_bytes: int = 100_000
    auto_push: bool = False


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    repository: str | Path
    expected_branch: str
    starting_commit: str
    approved_files: tuple[str, ...]
    job_id: int
    step_id: int
    title: str
    preexisting_files: tuple[str, ...] = ()
    marker: str | None = None
    protected_branches: tuple[str, ...] = ()
    auto_push: bool | None = None


@dataclass(slots=True)
class CheckpointResult:
    success: bool
    commit_sha: str | None
    marker: str
    staged_files: list[str] = field(default_factory=list)
    message: str = ""
    failure_code: str | None = None
    recovered: bool = False
    details: dict[str, object] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return self.message

    @property
    def error(self) -> str | None:
        return None if self.success else self.message

    @property
    def retryable(self) -> bool:
        """Whether a Coder revision is a safe default next action.

        Repository ownership/branch/history conflicts need an operator, while
        whitespace or secret findings can be repaired and reviewed again.
        """
        if self.success:
            return False
        message = self.message.lower()
        return "secret scan found" in message or "git diff --check failed" in message

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        # ``Path`` is allowed in requests but never leaks into result details.
        return value


@dataclass(frozen=True, slots=True)
class _StatusEntry:
    code: str
    paths: tuple[str, ...]


class CheckpointService:
    """Create an idempotent, explicit-file Git commit after final checks."""

    def __init__(
        self,
        config: CheckpointConfig | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> None:
        self.config = config or CheckpointConfig()
        self.secret_scanner = secret_scanner or SecretScanner()

    @staticmethod
    def default_marker(job_id: int, step_id: int) -> str:
        """Stable marker enables recovery after commit-before-DB crashes."""
        return f"Autonomous-Step: job-{job_id} step-{step_id}"

    def checkpoint(
        self,
        request: CheckpointRequest | None = None,
        *,
        repository: str | Path | None = None,
        branch: str | None = None,
        expected_branch: str | None = None,
        starting_commit: str | None = None,
        approved_files: Sequence[str] | None = None,
        preexisting_files: Sequence[str] = (),
        job_id: int | None = None,
        step_id: int | None = None,
        title: str = "",
        marker: str | None = None,
        protected_branches: Sequence[str] | None = None,
        auto_push: bool | None = None,
    ) -> CheckpointResult:
        """Commit one reviewed change set, or return a structured safe refusal.

        A :class:`CheckpointRequest` is the preferred typed API.  Keyword
        arguments mirror the Orchestrator's persisted work record so the
        service is also straightforward to invoke from a small CLI or a
        recovery path.  ``branch`` is an alias for ``expected_branch``.
        """
        if request is not None:
            if any(
                value is not None
                for value in (
                    repository,
                    branch,
                    expected_branch,
                    starting_commit,
                    approved_files,
                    job_id,
                    step_id,
                    marker,
                    protected_branches,
                    auto_push,
                )
            ) or preexisting_files or title:
                raise TypeError("provide either CheckpointRequest or checkpoint keyword arguments")
        else:
            selected_branch = expected_branch or branch or ""
            request = CheckpointRequest(
                repository=repository or "",
                expected_branch=selected_branch,
                starting_commit=starting_commit or "",
                approved_files=tuple(approved_files or ()),
                job_id=job_id if job_id is not None else 0,
                step_id=step_id if step_id is not None else 0,
                title=title,
                preexisting_files=tuple(preexisting_files),
                marker=marker,
                protected_branches=tuple(protected_branches or ()),
                auto_push=auto_push,
            )
        return self._checkpoint_request(request)

    def _checkpoint_request(self, request: CheckpointRequest) -> CheckpointResult:
        """Typed implementation of :meth:`checkpoint`."""
        marker = request.marker or self.default_marker(request.job_id, request.step_id)
        if not _MARKER_RE.fullmatch(marker):
            return self._failed(marker, "invalid_marker", "checkpoint marker is invalid")
        try:
            root = self._repository_root(request.repository)
            recovered = self.find_existing_checkpoint(root, marker)
            if recovered:
                return CheckpointResult(
                    success=True,
                    commit_sha=recovered,
                    marker=marker,
                    message="existing checkpoint recovered by marker",
                    recovered=True,
                )

            approved = self._safe_files(root, request.approved_files)
            preexisting = set(self._safe_files(root, request.preexisting_files))
            if not approved:
                raise CheckpointError("checkpoint requires explicitly approved files")
            if not _COMMIT_RE.fullmatch(request.starting_commit):
                raise CheckpointError("starting_commit must be a Git commit SHA")

            current_branch = self._git_text(root, "branch", "--show-current")
            configured_protected = request.protected_branches or self.config.protected_branches
            protected = {branch.lower() for branch in configured_protected}
            if not current_branch:
                raise CheckpointError("refusing checkpoint from a detached HEAD")
            if current_branch != request.expected_branch:
                raise CheckpointError(
                    f"branch mismatch: expected {request.expected_branch!r}, found {current_branch!r}"
                )
            if current_branch.lower() in protected or request.expected_branch.lower() in protected:
                raise CheckpointError(f"refusing to modify protected branch {current_branch!r}")

            starting = self._git_text(root, "rev-parse", "--verify", f"{request.starting_commit}^{{commit}}")
            head = self._git_text(root, "rev-parse", "HEAD")
            if head != starting:
                raise CheckpointError(
                    "HEAD changed since coding began; refusing to combine unreviewed history"
                )

            if self._git_text(root, "diff", "--cached", "--name-only", "-z"):
                raise CheckpointError(
                    "index already contains staged changes; preserving possible human work"
                )

            status_entries = self._status_entries(root)
            staged_paths = self._reviewed_status_paths(status_entries, set(approved), preexisting)
            if not staged_paths:
                raise CheckpointError("there are no reviewed working-tree changes to checkpoint")

            whitespace = self._run(root, ("git", "diff", "--check", starting, "--", *approved))
            if whitespace.returncode:
                raise CheckpointError(
                    "git diff --check failed: " + self._brief_output(whitespace.stderr or whitespace.stdout)
                )
            secret_hits = self._scan_candidate(root, starting, approved)
            if secret_hits:
                raise CheckpointError("secret scan found: " + ", ".join(sorted(set(secret_hits))))

            # Explicit paths plus -A supports deletions/renames without ever
            # staging unrelated files.  It is deliberately not `git add .`.
            add_result = self._run(root, ("git", "add", "-A", "--", *sorted(staged_paths)))
            if add_result.returncode:
                raise CheckpointError("git add failed: " + self._brief_output(add_result.stderr))
            staged_now = set(self._z_paths(self._git_text(root, "diff", "--cached", "--name-only", "-z")))
            if not staged_now:
                self._unstage(root, staged_paths)
                raise CheckpointError("git add staged no changes")
            if not staged_now.issubset(staged_paths):
                self._unstage(root, staged_paths)
                raise CheckpointError("staging included a path outside reviewed changes")

            subject = self._commit_subject(request.title, request.step_id)
            commit = self._run(
                root,
                (
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    subject,
                    "-m",
                    marker,
                ),
            )
            if commit.returncode:
                self._unstage(root, staged_paths)
                raise CheckpointError("git commit failed: " + self._brief_output(commit.stderr))
            sha = self._git_text(root, "rev-parse", "HEAD")
            message = self._git_text(root, "log", "-1", "--format=%B")
            if marker not in message:
                raise CheckpointError("new commit does not contain its checkpoint marker")
            push_requested = bool(
                request.auto_push if request.auto_push is not None else self.config.auto_push
            )
            push_details: dict[str, object] = {
                "auto_push": push_requested,
                "push_attempted": False,
            }
            message_text = "checkpoint commit created locally; automatic push is disabled"
            if push_requested:
                # This is deliberately a plain push: no --force, no history
                # rewriting, no branch selection inferred from model output.
                push = self._run(root, ("git", "push"))
                push_details["push_attempted"] = True
                push_details["push_exit_code"] = push.returncode
                if push.returncode:
                    push_details["push_error"] = self._brief_output(push.stderr or push.stdout)
                    message_text = "checkpoint commit created locally; automatic push failed"
                else:
                    message_text = "checkpoint commit created and pushed"
            return CheckpointResult(
                success=True,
                commit_sha=sha,
                marker=marker,
                staged_files=sorted(staged_now),
                message=message_text,
                details=push_details,
            )
        except CheckpointError as exc:
            return self._failed(marker, "checkpoint_refused", str(exc))
        except OSError as exc:
            return self._failed(marker, "git_unavailable", self.secret_scanner.redact(str(exc)))

    def find_existing_checkpoint(self, repository: str | Path, marker: str) -> str | None:
        """Return a prior matching commit SHA for idempotent crash recovery."""
        if not _MARKER_RE.fullmatch(marker):
            raise CheckpointError("checkpoint marker is invalid")
        root = self._repository_root(repository)
        result = self._run(
            root,
            ("git", "log", "--all", "--fixed-strings", f"--grep={marker}", "--format=%H", "-n", "2"),
        )
        if result.returncode:
            raise CheckpointError("cannot search Git history for checkpoint marker")
        matches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(matches) > 1:
            raise CheckpointError("checkpoint marker is not unique in Git history")
        return matches[0] if matches else None

    def _repository_root(self, repository: str | Path) -> Path:
        root = Path(repository).expanduser().resolve(strict=True)
        if not root.is_dir() or not (root / ".git").exists():
            raise CheckpointError("repository must be an existing Git working tree")
        return root

    @staticmethod
    def _safe_files(root: Path, values: Sequence[str]) -> list[str]:
        # Keep path validation identical to final verification.  The helper is
        # static and uses no command execution, so checkpointing is safe even
        # when final verification was interrupted before persistence.
        return VerificationService._safe_files(root, values)

    def _status_entries(self, root: Path) -> list[_StatusEntry]:
        raw = self._git_text(root, "status", "--porcelain=v1", "-z", "-uall")
        tokens = raw.split("\0")
        entries: list[_StatusEntry] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            if len(token) < 4:
                raise CheckpointError("unparseable git status entry")
            code, primary = token[:2], token[3:]
            paths = [primary]
            if "R" in code or "C" in code:
                if index >= len(tokens) or not tokens[index]:
                    raise CheckpointError("unparseable renamed git status entry")
                paths.append(tokens[index])
                index += 1
            safe = tuple(self._safe_files(root, paths))
            entries.append(_StatusEntry(code=code, paths=safe))
        return entries

    @staticmethod
    def _reviewed_status_paths(
        entries: Sequence[_StatusEntry], approved: set[str], preexisting: set[str]
    ) -> set[str]:
        staged: set[str] = set()
        seen_approved: set[str] = set()
        for entry in entries:
            paths = set(entry.paths)
            has_approved = bool(paths & approved)
            has_preexisting = bool(paths & preexisting)
            if has_approved and has_preexisting:
                raise CheckpointError(
                    "reviewed change overlaps pre-existing human changes; refusing to combine them"
                )
            if has_preexisting:
                # An entire rename/copy entry belongs to the human baseline.
                continue
            if has_approved:
                staged.update(paths)
                seen_approved.update(paths & approved)
                continue
            raise CheckpointError(
                "working tree contains an unreviewed change: " + ", ".join(sorted(paths))
            )
        missing = approved - seen_approved
        if missing:
            raise CheckpointError(
                "approved file is no longer a working-tree change: " + ", ".join(sorted(missing))
            )
        return staged

    def _scan_candidate(self, root: Path, starting_commit: str, files: Sequence[str]) -> list[str]:
        diff = self._run(
            root,
            ("git", "diff", "--no-ext-diff", starting_commit, "--", *files),
            redact_output=False,
        )
        findings = self.secret_scanner.scan_text(diff.stdout)
        for relative in files:
            path = root / relative
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()[:1_000_000].decode("utf-8", "replace")
            except OSError:
                continue
            findings.extend(self.secret_scanner.scan_text(content))
        return findings

    def _git_text(self, root: Path, *argv: str) -> str:
        result = self._run(root, ("git", *argv))
        if result.returncode:
            raise CheckpointError(self._brief_output(result.stderr) or "git command failed")
        return result.stdout.strip() if "-z" not in argv else result.stdout

    def _run(
        self,
        root: Path,
        argv: Iterable[str],
        *,
        redact_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=root,
                env=VerificationService._safe_environment(),
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                list(argv),
                124,
                self._as_text(exc.stdout),
                self._as_text(exc.stderr) + "\ncommand timed out",
            )
        if redact_output:
            completed.stdout = self.secret_scanner.redact(completed.stdout or "")
            completed.stderr = self.secret_scanner.redact(completed.stderr or "")
        return completed

    def _unstage(self, root: Path, paths: Iterable[str]) -> None:
        # Only the explicit paths staged by this service are reset.  This
        # changes the index, not working-tree content or history.
        self._run(root, ("git", "restore", "--staged", "--", *sorted(set(paths))))

    @staticmethod
    def _z_paths(raw: str) -> list[str]:
        return [part for part in raw.split("\0") if part]

    @staticmethod
    def _commit_subject(title: str, step_id: int) -> str:
        compact = " ".join(title.split())
        if not compact:
            compact = f"Complete approved step {step_id}"
        return ("agent: " + compact)[:72]

    def _brief_output(self, value: str) -> str:
        compact = " ".join(self.secret_scanner.redact(value).split())
        return compact[:500]

    @staticmethod
    def _as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value

    @staticmethod
    def _failed(marker: str, code: str, message: str) -> CheckpointResult:
        return CheckpointResult(
            success=False,
            commit_sha=None,
            marker=marker,
            message=message,
            failure_code=code,
        )
