from __future__ import annotations

import os
from pathlib import Path


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
        resolved = candidate.resolve(strict=must_exist)
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes repository: {relative}")
        return resolved

    def read_text(self, relative: str, max_bytes: int = 200_000) -> str:
        path = self.resolve(relative, must_exist=True)
        if path.stat().st_size > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} byte read limit")
        return path.read_text(encoding="utf-8")

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
                return path.read_text(encoding="utf-8")
        return ""

