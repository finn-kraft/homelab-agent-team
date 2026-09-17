from __future__ import annotations

import os
from pathlib import Path

from agent_core.prompt_budget import bounded_text


class WorkspaceViolation(ValueError):
    pass


class Workspace:
    """Resolves every path through a single real repository root."""

    def __init__(self, repository: str | Path, allowed_roots: list[str | Path]):
        self.root = Path(repository).resolve(strict=True)
        roots = [Path(p).resolve(strict=True) for p in allowed_roots]
        if not any(self.root == root or self.root.is_relative_to(root) for root in roots):
            raise WorkspaceViolation(f"repository is outside configured roots: {self.root}")
        if not (self.root / ".git").exists():
            raise WorkspaceViolation(f"not a git repository: {self.root}")

    def resolve(self, relative: str | Path, *, must_exist: bool = False) -> Path:
        candidate = self.root / relative
        # resolve(strict=False) still resolves every existing symlink component.
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise WorkspaceViolation(
                f"path {str(relative)!r} does not exist in the repository; "
                "inspect the repository tree before reading it"
            ) from exc
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes repository: {relative}")
        return resolved

    def relative_path(self, relative: str | Path) -> str:
        """Return a canonical repository-relative path for an agent action.

        Git reports paths without a leading ``./`` while models sometimes
        return ``./file.py`` or use redundant ``..`` segments. Canonicalizing
        at the workspace boundary keeps the human-change safety check aligned
        with Git and prevents an alternate spelling from bypassing it.
        """
        resolved = self.resolve(relative)
        if resolved == self.root:
            raise WorkspaceViolation("repository root is not a file path")
        return resolved.relative_to(self.root).as_posix()

    def read_text(self, relative: str, max_bytes: int = 200_000) -> str:
        path = self.resolve(relative, must_exist=True)
        if not path.is_file():
            raise WorkspaceViolation(
                f"path {relative!r} is not a file in the repository; "
                "inspect the repository tree and read a file path"
            )
        if path.stat().st_size > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} byte read limit")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceViolation(
                f"path {relative!r} is not a UTF-8 text file; use an inspection command"
            ) from exc

    def write_text(self, relative: str, content: str) -> None:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def delete(self, relative: str, justification: str) -> None:
        if len(justification.strip()) < 12:
            raise WorkspaceViolation("file deletion requires an explicit justification")
        path = self.resolve(relative, must_exist=True)
        if path.is_dir():
            raise WorkspaceViolation("recursive directory deletion is not supported")
        path.unlink()

    def project_instructions(self) -> str:
        for name in ("WORKER.md", "AGENTS.md"):
            path = self.root / name
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content) > 100_000:
                    return bounded_text(content, 100_000, "[PROJECT INSTRUCTIONS TRUNCATED]")
                return content
        return ""

    def list_files(self, max_entries: int = 1_000) -> list[str]:
        """Return a bounded, relative file list for model path selection.

        The EngineeringAgent must not guess paths from the absolute repository
        label. This read-only inventory gives it concrete names before its
        first ``read`` action while keeping prompts bounded.
        """
        entries: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if len(entries) >= max_entries:
                break
            if not path.is_file() or ".git" in path.parts:
                continue
            entries.append(path.relative_to(self.root).as_posix())
        return entries
