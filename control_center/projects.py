from __future__ import annotations
import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    repository: str
    branch: str
    roadmap: str | None = "docs/roadmap.md"

class ProjectCatalog:
    """Explicit allowlist of repositories exposed through the web interface."""
    def __init__(self, projects: list[Project]):
        if not projects: raise ValueError("at least one authorized project is required")
        self._projects = {project.id: project for project in projects}
        if len(self._projects) != len(projects): raise ValueError("project IDs must be unique")
        for project in projects:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project.id):
                raise ValueError(f"invalid project ID: {project.id}")
    @classmethod
    def from_env(cls):
        raw = os.getenv("CONTROL_CENTER_PROJECTS")
        if not raw: raise RuntimeError("CONTROL_CENTER_PROJECTS is required")
        value = json.loads(raw)
        if not isinstance(value, list): raise ValueError("CONTROL_CENTER_PROJECTS must be a JSON list")
        return cls([Project(**item) for item in value])
    def get(self, project_id):
        try: return self._projects[project_id]
        except KeyError as exc: raise KeyError(f"unknown authorized project: {project_id}") from exc
    def list(self): return [self.inspect(project) for project in self._projects.values()]
    @staticmethod
    def _git(root, *args):
        done = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                              timeout=10, shell=False)
        return done.stdout.strip() if done.returncode == 0 else None
    def inspect(self, project):
        result = asdict(project); root = Path(project.repository).resolve(strict=False)
        result.update({"available": False, "git_status": "unavailable", "latest_commit": None,
                       "roadmap_present": False})
        if not root.is_dir() or not (root / ".git").exists(): return result
        status = self._git(root, "status", "--short")
        latest = self._git(root, "log", "-1", "--format=%H%x00%s%x00%cI")
        commit = None
        if latest:
            parts = latest.split("\x00", 2)
            if len(parts) == 3: commit = {"sha":parts[0],"message":parts[1],"timestamp":parts[2]}
        result.update({
            "available": True,
            "git_status": "clean" if status == "" else "dirty",
            "current_branch": self._git(root, "branch", "--show-current"),
            "latest_commit": commit,
            "roadmap_present": bool(project.roadmap and (root / project.roadmap).is_file()),
        })
        return result
