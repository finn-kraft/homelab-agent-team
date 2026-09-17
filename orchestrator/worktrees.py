from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str
    starting_commit: str
    created: bool = True


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

    def _records(self, repository: Path) -> list[dict[str, str]]:
        """Read Git's authoritative worktree registry."""
        output = self._run(repository, "worktree", "list", "--porcelain")
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line:
                if current.get("path"):
                    records.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["path"] = value
            elif key == "HEAD":
                current["commit"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
        return records

    def managed_records(self, repository: str | Path) -> list[dict[str, str]]:
        """Return worktrees rooted below the manager directory."""
        repo = Path(repository).resolve(strict=True)
        return [record for record in self._records(repo)
                if Path(record["path"]).resolve().is_relative_to(self.root)]

    def orphan_records(self, repository: str | Path,
                       referenced_paths: Iterable[str | Path]) -> list[dict[str, str]]:
        """Find managed worktrees no longer referenced by durable packages."""
        referenced = {Path(value).expanduser().resolve() for value in referenced_paths}
        return [record for record in self.managed_records(repository)
                if Path(record["path"]).resolve() not in referenced]

    def cleanup_orphans(self, repository: str | Path,
                        referenced_paths: Iterable[str | Path]) -> list[Path]:
        """Remove only orphaned worktrees inside the explicitly managed root."""
        repo = Path(repository).resolve(strict=True)
        removed: list[Path] = []
        for record in self.orphan_records(repo, referenced_paths):
            path = Path(record["path"]).resolve()
            self.remove(repo, path)
            removed.append(path)
        return removed

    def create(self, repository: str | Path, mission_id: int, package_id: int,
               base_branch: str) -> Worktree:
        repo = Path(repository).resolve(strict=True)
        if not (repo / ".git").exists():
            raise ValueError(f"not a Git repository: {repo}")
        branch = f"agents/mission-{mission_id}/package-{package_id}"
        path = self.root / f"mission-{mission_id}" / f"package-{package_id}"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self._records(repo)
        resolved_path = path.resolve()
        for record in records:
            record_path = Path(record["path"]).resolve()
            if record_path == resolved_path:
                if record.get("branch") != branch:
                    raise RuntimeError(
                        f"worktree path is registered to branch {record.get('branch')!r}, "
                        f"expected {branch!r}"
                    )
                return Worktree(path, branch,
                                record.get("commit") or self._run(path, "rev-parse", "HEAD"),
                                created=False)
            if record.get("branch") == branch:
                raise RuntimeError(
                    f"branch collision: {branch!r} is already checked out at {record_path}"
                )
        if path.exists():
            raise FileExistsError(f"worktree path exists but is not a registered worktree: {path}")
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo, check=False,
        ).returncode == 0
        if branch_exists:
            add_args = ("worktree", "add", str(path), branch)
        else:
            add_args = ("worktree", "add", "-b", branch, str(path), base_branch)
        try:
            self._run(repo, *add_args)
            starting_commit = self._run(path, "rev-parse", "HEAD")
        except Exception:
            # A Git worktree can be created successfully while a later
            # database update fails. Clean that just-created path here so a
            # retried package does not inherit an orphan or branch collision.
            if path.exists():
                try:
                    self.remove(repo, path)
                except Exception:
                    pass
            raise
        return Worktree(path, branch, starting_commit, created=True)

    def remove(self, repository: str | Path, worktree: str | Path) -> None:
        repo = Path(repository).resolve(strict=True)
        path = Path(worktree).resolve()
        if path == repo or not path.is_relative_to(self.root):
            raise ValueError("worktree is outside the managed worktree root")
        self._run(repo, "worktree", "remove", "--force", str(path))
