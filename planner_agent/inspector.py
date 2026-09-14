from __future__ import annotations

import subprocess
from pathlib import Path


class InspectionError(ValueError):
    pass


class ReadOnlyRepositoryInspector:
    """A deliberately read-only, bounded view of an allowed Git repository."""

    DOCUMENTS = ("WORKER.md", "AGENTS.md", "README.md", "docs/roadmap.md",
                 "docs/architecture.md", "ARCHITECTURE.md")

    def __init__(self, allowed_roots: list[str], max_file_bytes: int = 100_000,
                 max_tree_entries: int = 500):
        self.allowed_roots = [Path(root).resolve(strict=True) for root in allowed_roots]
        self.max_file_bytes = max_file_bytes
        self.max_tree_entries = max_tree_entries

    def resolve_repository(self, repository: str) -> Path:
        root = Path(repository).resolve(strict=True)
        if not any(root == allowed or root.is_relative_to(allowed)
                   for allowed in self.allowed_roots):
            raise InspectionError("repository is outside configured roots")
        if not (root / ".git").exists():
            raise InspectionError("configured path is not a Git repository")
        return root

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=root, text=True,
                                capture_output=True, timeout=15, shell=False)
        if result.returncode:
            raise InspectionError(result.stderr.strip() or "Git inspection failed")
        return result.stdout[:100_000]

    def inspect(self, repository: str, branch: str) -> dict:
        root = self.resolve_repository(repository)
        actual_branch = self._git(root, "branch", "--show-current").strip()
        documents: dict[str, str] = {}
        for relative in self.DOCUMENTS:
            path = (root / relative).resolve()
            if path.is_file() and path.is_relative_to(root) and path.stat().st_size <= self.max_file_bytes:
                documents[relative] = path.read_text(encoding="utf-8", errors="replace")
        tracked = self._git(root, "ls-files").splitlines()[:self.max_tree_entries]
        return {
            "configured_branch": branch,
            "actual_branch": actual_branch,
            "branch_matches": actual_branch == branch,
            "head": self._git(root, "rev-parse", "HEAD").strip(),
            "git_status": self._git(root, "status", "--short").splitlines(),
            "recent_history": self._git(root, "log", "-10", "--oneline").splitlines(),
            "repository_files": tracked,
            "documents": documents,
        }

