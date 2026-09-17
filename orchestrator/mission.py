from __future__ import annotations

"""Mission-level planning for V2.

The Planner still owns the individual implementation prompt.  This small
manager owns the durable roadmap boundary: it turns unchecked roadmap items
into idempotent Work Packages and keeps mission status in sync with them.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CHECKBOX = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")


@dataclass(frozen=True, slots=True)
class RoadmapItem:
    line: int
    text: str
    reference: str


def parse_roadmap(text: str, *, reference: str = "docs/roadmap.md") -> list[RoadmapItem]:
    """Extract unfinished checklist items without asking a model to re-plan."""
    items: list[RoadmapItem] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = _CHECKBOX.match(line)
        if not match or match.group("mark").lower() == "x":
            continue
        text_value = " ".join(match.group("text").split())
        if text_value:
            items.append(RoadmapItem(line_number, text_value, f"{reference}:{line_number}"))
    return items


class MissionManager:
    def __init__(self, store: Any, *, max_packages: int = 3):
        self.store = store
        self.max_packages = max(1, int(max_packages))

    def discover(self, repository: str | Path, roadmap: str = "docs/roadmap.md") -> list[RoadmapItem]:
        root = Path(repository).expanduser().resolve(strict=True)
        path = (root / roadmap).resolve()
        if not path.is_relative_to(root):
            raise ValueError("roadmap must be inside the repository")
        return parse_roadmap(path.read_text(encoding="utf-8"), reference=roadmap)

    def ensure_packages(self, mission_id: int, *, repository: str | Path | None = None,
                        roadmap: str = "docs/roadmap.md", branch: str | None = None,
                        limit: int | None = None) -> list[int]:
        mission = self.store.mission_detail(mission_id)
        if not mission:
            raise KeyError(mission_id)
        repo = str(repository or mission["repository"])
        branch_name = branch or mission["branch"]
        existing = mission.get("packages", [])
        existing_refs = {str(p.get("roadmap_reference")) for p in existing if p.get("roadmap_reference")}
        existing_objectives = {str(p.get("objective", "")).strip().casefold() for p in existing}
        count = self.max_packages if limit is None else max(0, int(limit))
        created: list[int] = []
        for item in self.discover(repo, roadmap):
            if len(created) >= count:
                break
            if item.reference in existing_refs or item.text.casefold() in existing_objectives:
                continue
            package_id = self.store.create_work_package(
                mission_id,
                item.text,
                repo,
                branch_name,
                [f"Implement roadmap item: {item.text}", "Project verification passes"],
                constraints=["Keep the change reviewable and scoped to this work package"],
                roadmap_reference=item.reference,
            )
            created.append(package_id)
            existing_refs.add(item.reference)
            existing_objectives.add(item.text.casefold())
        self.refresh_status(mission_id, roadmap=roadmap)
        return created

    def refresh_status(self, mission_id: int, *, roadmap: str = "docs/roadmap.md") -> str:
        mission = self.store.mission_detail(mission_id)
        if not mission:
            raise KeyError(mission_id)
        packages = mission.get("packages", [])
        evidence_getter = getattr(self.store, "package_completion_evidence", None)
        evidence_by_package: dict[int, dict[str, Any]] = {}
        if evidence_getter is not None:
            for package in packages:
                try:
                    evidence_by_package[int(package["id"])] = evidence_getter(int(package["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        def is_complete(package: dict[str, Any]) -> bool:
            if package.get("status") != "complete":
                return False
            evidence = evidence_by_package.get(int(package["id"])) if evidence_getter is not None else None
            return bool(evidence["eligible"]) if evidence is not None else evidence_getter is None

        completed_refs = {
            str(package.get("roadmap_reference"))
            for package in packages if package.get("roadmap_reference") and is_complete(package)
        }
        if packages and all(is_complete(package) for package in packages):
            remaining = [item for item in self.discover(mission["repository"], roadmap)
                         if item.reference not in completed_refs]
            status = "active" if remaining else "complete"
        elif any(p.get("status") in {"blocked", "failed"} for p in packages):
            status = "blocked"
        elif mission.get("status") not in {"paused", "cancelled"}:
            status = "active"
        else:
            status = mission["status"]
        if status != mission.get("status"):
            self.store.update_mission_status(mission_id, status)
        return status
