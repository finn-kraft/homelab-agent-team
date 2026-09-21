from __future__ import annotations

import subprocess

import pytest

from orchestrator.integration import IntegrationManager
from orchestrator.mission import MissionManager, parse_roadmap


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def test_parse_roadmap_only_returns_unchecked_items():
    items = parse_roadmap("""# Roadmap\n- [x] shipped\n- [ ]  Add API   health\n  - [ ] nested item\n- [ ] docs\n""")
    assert [item.text for item in items] == ["Add API health", "nested item", "docs"]
    assert items[0].reference == "docs/roadmap.md:3"


def test_mission_manager_materializes_idempotently(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "roadmap.md").write_text("- [ ] First package\n- [ ] Second package\n")

    class Store:
        def __init__(self):
            self.packages = []
            self.next_id = 1
            self.status = "active"

        def mission_detail(self, _id):
            return {"id": 1, "repository": str(repo), "branch": "agents/work",
                    "status": self.status, "packages": list(self.packages)}

        def create_work_package(self, mission_id, objective, repository, branch,
                                acceptance_criteria, constraints, roadmap_reference):
            package = {"id": self.next_id, "mission_id": mission_id, "objective": objective,
                       "repository": repository, "branch": branch, "status": "ready",
                       "roadmap_reference": roadmap_reference}
            self.next_id += 1
            self.packages.append(package)
            return package["id"]

        def update_mission_status(self, _id, status):
            self.status = status

    store = Store()
    manager = MissionManager(store, max_packages=2)
    assert manager.ensure_packages(1) == [1, 2]
    assert manager.ensure_packages(1) == []
    assert len(store.packages) == 2


def test_mission_manager_requires_integration_evidence_for_completion(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "roadmap.md").write_text("- [ ] Ship the feature\n")

    class Store:
        def __init__(self):
            self.status = "active"
            self.evidence = {"eligible": False}
            self.packages = [{"id": 1, "status": "complete",
                              "roadmap_reference": "docs/roadmap.md:1"}]

        def mission_detail(self, _id):
            return {"id": 1, "repository": str(repo), "branch": "agents/work",
                    "status": self.status, "packages": self.packages}

        def package_completion_evidence(self, _id):
            return self.evidence

        def update_mission_status(self, _id, status):
            self.status = status

    store = Store()
    manager = MissionManager(store)
    assert manager.refresh_status(1) == "active"
    assert store.status == "active"
    store.evidence = {"eligible": True}
    assert manager.refresh_status(1) == "complete"
    assert store.status == "complete"


def test_integration_marks_roadmap_after_verified_package(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n- [ ] Ship the feature\n")
    subprocess.run(["git", "init", "-q", "-b", "mission"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    git(repo, "checkout", "-qb", "agents/package-1")
    (repo / "feature.txt").write_text("implemented\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "feature"], cwd=repo, check=True)
    feature = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "mission")

    class Store:
        def __init__(self): self.records = []
        def package_completion_evidence(self, _id):
            return {"eligible": False, "ready_for_integration": True,
                    "checks": {"package_complete": True, "review_approved": True,
                                "verification_passed": True, "checkpoint_complete": True,
                                "integration_complete": False}}
        def mission_detail(self, _id):
            return {"repository": str(repo), "branch": "mission", "status": "active", "packages": []}
        def upsert_integration(self, **kwargs): self.records.append(kwargs)

    store = Store()
    result = IntegrationManager(store).integrate(
        {"id": 1, "mission_id": 1, "status": "complete", "branch": "agents/package-1",
         "roadmap_reference": "docs/roadmap.md:2", "resulting_commit": feature}, repo
    )
    assert result["status"] == "complete"
    assert result["roadmap_marked"] is True
    assert "[x] Ship the feature" in git(repo, "show", "mission:docs/roadmap.md")
    assert store.records[-1]["status"] == "complete"


def test_integration_rejects_package_without_review_verification_checkpoint():
    class Store:
        def package_completion_evidence(self, _id):
            return {"eligible": False, "ready_for_integration": False,
                    "checks": {"package_complete": True, "review_approved": False,
                                "verification_passed": False, "checkpoint_complete": False,
                                "integration_complete": False}}

    with pytest.raises(ValueError, match="review_approved"):
        IntegrationManager(Store()).integrate(
            {"id": 1, "mission_id": 1, "status": "complete", "resulting_commit": "abc1234"},
            "/tmp",
        )


def test_integration_manager_merges_verified_commit_without_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "mission"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "agents/package-1")
    (repo / "feature.txt").write_text("implemented\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "feature"], cwd=repo, check=True)
    feature = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "mission")

    class Store:
        def __init__(self): self.records = []
        def mission_detail(self, _id): return {"branch": "mission"}
        def upsert_integration(self, **kwargs): self.records.append(kwargs)

    store = Store()
    result = IntegrationManager(store).integrate(
        {"id": 1, "mission_id": 1, "branch": "agents/package-1", "resulting_commit": feature}, repo
    )
    assert result["status"] == "complete"
    assert git(repo, "rev-parse", "mission") != base
    assert "implemented" in git(repo, "show", "mission:feature.txt")
    assert store.records[-1]["status"] == "complete"


def test_integration_manager_refuses_protected_branch(tmp_path):
    class Store:
        def mission_detail(self, _id): return {"branch": "main"}
        def upsert_integration(self, **kwargs): pytest.fail("must not write an integration record")

    with pytest.raises(ValueError, match="protected"):
        IntegrationManager(Store()).integrate(
            {"id": 1, "mission_id": 1, "branch": "agents/x", "resulting_commit": "abc1234"}, tmp_path
        )


def test_integration_conflict_creates_human_escalation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "mission"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    git(repo, "checkout", "-qb", "agents/conflict")
    (repo / "README.md").write_text("package\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "package"], cwd=repo, check=True)
    feature = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "mission")
    (repo / "README.md").write_text("mission\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "mission"], cwd=repo, check=True)

    class Store:
        def __init__(self): self.records = []; self.human = []; self.mission_status = None
        def mission_detail(self, _id): return {"branch": "mission"}
        def upsert_integration(self, **kwargs): self.records.append(kwargs)
        def enqueue_human_request(self, **kwargs): self.human.append(kwargs)
        def update_mission_status(self, _id, status): self.mission_status = status

    store = Store()
    result = IntegrationManager(store).integrate(
        {"id": 2, "mission_id": 1, "job_id": 9, "step_id": 3,
         "status": "complete", "branch": "agents/conflict", "resulting_commit": feature}, repo
    )
    assert result["status"] == "conflict"
    assert store.records[-1]["status"] == "conflict"
    assert store.human[0]["kind"] == "integration-conflict"
    assert store.human[0]["job_id"] == 9
    assert store.mission_status == "blocked"
