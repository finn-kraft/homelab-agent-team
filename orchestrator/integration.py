from __future__ import annotations

"""Safe, explicit integration of verified package commits."""

import subprocess
import tempfile
from pathlib import Path
import re
from typing import Any


class IntegrationManager:
    def __init__(self, store: Any, *, protected_branches: tuple[str, ...] = ("main", "master")):
        self.store = store
        self.protected_branches = set(protected_branches)

    @staticmethod
    def _git(repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True,
                                timeout=120, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout.strip()

    @staticmethod
    def _mark_roadmap_item(worktree: Path, reference: str | None,
                           objective: str | None = None) -> str | None:
        """Check off one roadmap line in the branch being integrated.

        The reference is stored as ``path/to/roadmap.md:line`` by the
        deterministic roadmap parser.  Only that exact checkbox is changed,
        and the resulting marker is committed in the mission branch so the
        completion is reviewable and reversible.
        """
        if not reference:
            return None
        match = re.match(r"^(?P<path>.+):(?P<line>[0-9]+)$", str(reference))
        if not match:
            raise ValueError(f"invalid roadmap reference: {reference}")
        relative = Path(match.group("path"))
        roadmap = (worktree / relative).resolve()
        root = worktree.resolve()
        if not roadmap.is_relative_to(root):
            raise ValueError("roadmap reference escapes the integration worktree")
        if not roadmap.is_file():
            raise ValueError(f"roadmap file does not exist: {relative}")
        lines = roadmap.read_text(encoding="utf-8").splitlines(keepends=True)
        index = int(match.group("line")) - 1
        if index < 0 or index >= len(lines):
            raise ValueError(f"roadmap line is out of range: {reference}")
        if objective:
            normalise = lambda value: " ".join(str(value).split()).casefold()
            if normalise(objective) not in normalise(lines[index]):
                raise ValueError(f"roadmap reference does not match package objective: {reference}")
        if not re.search(r"\[[ xX]\]", lines[index]):
            raise ValueError(f"roadmap line is not a checkbox: {reference}")
        updated = re.sub(r"\[[ ]\]", "[x]", lines[index], count=1)
        if updated == lines[index]:
            return None
        lines[index] = updated
        roadmap.write_text("".join(lines), encoding="utf-8")
        IntegrationManager._git(worktree, "add", "--", str(relative))
        IntegrationManager._git(
            worktree, "-c", "user.name=Homelab Agent Team",
            "-c", "user.email=agent@homelab.local", "commit", "-m",
            f"chore: mark roadmap item complete ({reference})",
        )
        return IntegrationManager._git(worktree, "rev-parse", "HEAD")

    def integrate(self, package: dict[str, Any], repository: str | Path,
                  *, target_branch: str | None = None) -> dict[str, Any]:
        package_id = int(package["id"])
        mission_id = int(package["mission_id"])
        package_status = package.get("status")
        if package_status is not None and package_status != "complete":
            raise ValueError(
                f"package {package_id} is not verified and complete (status={package_status})"
            )
        evidence_getter = getattr(self.store, "package_completion_evidence", None)
        if evidence_getter is not None:
            evidence = evidence_getter(package_id)
            if not evidence.get("ready_for_integration", evidence.get("eligible")):
                missing = [name for name, present in evidence.get("checks", {}).items() if not present]
                raise ValueError(
                    f"package {package_id} lacks completion evidence: {', '.join(missing)}"
                )
        source_branch = str(package.get("branch") or "")
        target = target_branch or str(package.get("target_branch") or "")
        if not target:
            mission = self.store.mission_detail(mission_id)
            target = str(mission["branch"]) if mission else ""
        if target.lower() in {branch.lower() for branch in self.protected_branches}:
            raise ValueError(f"refusing to integrate directly into protected branch {target}")
        root = Path(repository).expanduser().resolve(strict=True)
        if not (root / ".git").exists():
            raise ValueError(f"not a Git repository: {root}")
        source_commit = str(package.get("resulting_commit") or "")
        if not source_commit and package.get("worktree"):
            source_commit = self._git(Path(str(package["worktree"])), "rev-parse", "HEAD")
        if not source_commit:
            raise ValueError("package has no verified resulting commit")
        self.store.upsert_integration(mission_id=mission_id, package_id=package_id,
                                      source_branch=source_branch, target_branch=target,
                                      status="integrating", commit_sha=source_commit)
        worktree: Path | None = None
        try:
            base = self._git(root, "rev-parse", target)
            worktree = Path(tempfile.mkdtemp(prefix=f".agent-integration-{package_id}-", dir=root))
            self._git(root, "worktree", "add", "--detach", str(worktree), target)
            merge = subprocess.run(["git", "-c", "user.name=Homelab Agent Team",
                                    "-c", "user.email=agent@homelab.local", "merge",
                                    "--no-ff", "--no-edit", source_commit],
                                   cwd=worktree, text=True, capture_output=True, timeout=300, check=False)
            if merge.returncode:
                self._git(worktree, "merge", "--abort", check=False)
                self.store.upsert_integration(mission_id=mission_id, package_id=package_id,
                                              source_branch=source_branch, target_branch=target,
                                              status="conflict", commit_sha=source_commit,
                                              error=(merge.stderr or merge.stdout)[-4000:])
                enqueue = getattr(self.store, "enqueue_human_request", None)
                if enqueue is not None:
                    enqueue(
                        mission_id=mission_id,
                        job_id=package.get("job_id"),
                        step_id=package.get("step_id"),
                        kind="integration-conflict",
                        question=(
                            f"Resolve the merge conflict while integrating package {package_id} "
                            f"into {target}, then retry integration."
                        ),
                        context={
                            "package_id": package_id,
                            "source_branch": source_branch,
                            "target_branch": target,
                            "source_commit": source_commit,
                            "merge_output": (merge.stderr or merge.stdout)[-4_000:],
                        },
                    )
                update_mission = getattr(self.store, "update_mission_status", None)
                if update_mission is not None:
                    update_mission(mission_id, "blocked")
                return {"status": "conflict", "package_id": package_id,
                        "target_branch": target, "error": (merge.stderr or merge.stdout).strip()}
            roadmap_commit = self._mark_roadmap_item(
                worktree, package.get("roadmap_reference"), package.get("objective")
            )
            merged = self._git(worktree, "rev-parse", "HEAD")
            self._git(root, "update-ref", f"refs/heads/{target}", merged, base)
            self.store.upsert_integration(mission_id=mission_id, package_id=package_id,
                                          source_branch=source_branch, target_branch=target,
                                          status="complete", commit_sha=merged)
            try:
                from .mission import MissionManager
                roadmap = str(package.get("roadmap_reference") or "docs/roadmap.md").rsplit(":", 1)[0]
                MissionManager(self.store).refresh_status(mission_id, roadmap=roadmap)
            except Exception:
                # Integration is durable and authoritative; a read-model
                # refresh can safely be retried by the next mission pass.
                pass
            return {"status": "complete", "package_id": package_id,
                    "target_branch": target, "commit_sha": merged,
                    "roadmap_marked": bool(roadmap_commit)}
        except Exception as exc:
            self.store.upsert_integration(mission_id=mission_id, package_id=package_id,
                                          source_branch=source_branch, target_branch=target,
                                          status="failed", commit_sha=source_commit, error=str(exc))
            raise
        finally:
            if worktree is not None:
                self._git(root, "worktree", "remove", "--force", str(worktree), check=False)
                if worktree.exists():
                    worktree.rmdir()
