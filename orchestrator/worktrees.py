from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str
    starting_commit: str


class WorktreeManager:
    """Create disposable, clean Git worktrees for autonomous packages."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _run(repository: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repository, text=True,
                                capture_output=True, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git worktree operation failed")
        return result.stdout.strip()

    def create(self, repository: str | Path, mission_id: int, package_id: int,
               base_branch: str) -> Worktree:
        repo = Path(repository).resolve(strict=True)
        if not (repo / ".git").exists():
            raise ValueError(f"not a Git repository: {repo}")
        branch = f"agents/mission-{mission_id}/package-{package_id}"
        path = self.root / f"mission-{mission_id}" / f"package-{package_id}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"worktree already exists: {path}")
        self._run(repo, "worktree", "add", "-b", branch, str(path), base_branch)
        starting_commit = self._run(path, "rev-parse", "HEAD")
        return Worktree(path, branch, starting_commit)

    def remove(self, repository: str | Path, worktree: str | Path) -> None:
        repo = Path(repository).resolve(strict=True)
        path = Path(worktree).resolve()
        if path == repo or not path.is_relative_to(self.root):
            raise ValueError("worktree is outside the managed worktree root")
        self._run(repo, "worktree", "remove", "--force", str(path))
