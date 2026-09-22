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
            elif key in {"locked", "prunable"}:
                current[key] = value or "true"
        return records

    def _managed_path(self, worktree: str | Path) -> Path:
        path = Path(worktree).expanduser().resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise ValueError("worktree is outside the managed worktree root")
        return path

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
        """Explicitly remove proven orphans inside the managed root.

        This method is intentionally not used by background reconciliation.
        It exists for a confirmed operator cleanup action.
        """
        repo = Path(repository).resolve(strict=True)
        removed: list[Path] = []
        for record in self.orphan_records(repo, referenced_paths):
            path = Path(record["path"]).resolve()
            self.remove(repo, path)
            removed.append(path)
        return removed

    def diagnose(self, repository: str | Path, worktree: str | Path) -> dict[str, object]:
        """Inspect registration and Git health without exposing repository content."""
        repo = Path(repository).resolve(strict=True)
        path = self._managed_path(worktree)
        records = self._records(repo)
        record = next(
            (item for item in records if Path(item["path"]).resolve() == path),
            None,
        )
        exists = path.exists()
        report: dict[str, object] = {
            "path": str(path),
            "path_exists": exists,
            "registered": record is not None,
            "git_metadata_valid": False,
            "dirty": None,
            "dirty_entries": None,
            "branch": record.get("branch") if record else None,
            "head": record.get("commit") if record else None,
            "locked": bool(record and record.get("locked")),
            "prunable": bool(record and record.get("prunable")),
        }
        if not exists and record:
            report.update({
                "classification": "registered_missing",
                "recommendation": "preserve database evidence; inspect the path before pruning registration",
            })
            return report
        if not exists:
            report.update({
                "classification": "missing",
                "recommendation": "recreate only through the normal package claim path",
            })
            return report
        if not record:
            report.update({
                "classification": "unregistered_path",
                "recommendation": "preserve the directory and use confirmed repair only if it has Git metadata",
            })
            return report

        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode or probe.stdout.strip() != "true":
            report.update({
                "classification": "corrupt_git_metadata",
                "recommendation": "run the confirmed metadata-only repair and diagnose again",
            })
            return report
        report["git_metadata_valid"] = True
        status = self._run(path, "status", "--porcelain", "--untracked-files=normal")
        dirty_entries = len(status.splitlines()) if status else 0
        report["dirty"] = dirty_entries > 0
        report["dirty_entries"] = dirty_entries
        report["classification"] = "dirty" if dirty_entries else "healthy"
        report["recommendation"] = (
            "preserve and review changes before any cleanup"
            if dirty_entries
            else "no recovery action is needed"
        )
        return report

    def repair(
        self,
        repository: str | Path,
        worktree: str | Path,
        *,
        confirm: bool = False,
    ) -> dict[str, object]:
        """Run Git's metadata-only repair after explicit operator confirmation.

        It never invokes reset, clean, checkout, prune, or worktree removal.
        """
        if not confirm:
            raise ValueError("worktree repair requires explicit confirmation")
        repo = Path(repository).resolve(strict=True)
        path = self._managed_path(worktree)
        if not path.exists():
            raise RuntimeError("worktree path is missing; refusing automatic reconstruction")
        if not (path / ".git").exists():
            raise RuntimeError("path does not contain Git worktree metadata; refusing repair")
        self._run(repo, "worktree", "repair", str(path))
        return self.diagnose(repo, path)

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
        path = self._managed_path(worktree)
        self._run(repo, "worktree", "remove", "--force", str(path))
