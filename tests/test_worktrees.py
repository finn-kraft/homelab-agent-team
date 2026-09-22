import subprocess
from pathlib import Path

import pytest

from orchestrator.worktrees import WorktreeManager


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def test_worktree_manager_creates_isolated_package(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "README").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-m", "seed")
    worktree = WorktreeManager(tmp_path / "worktrees").create(repo, 1, 2, "main")
    assert worktree.branch == "agents/mission-1/package-2"
    assert worktree.path.exists() and len(worktree.starting_commit) == 40
    assert git(worktree.path, "branch", "--show-current") == worktree.branch
    WorktreeManager(tmp_path / "worktrees").remove(repo, worktree.path)
    assert not worktree.path.exists()


def test_worktree_manager_reuses_registered_tree_after_claim_retry(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "README").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-m", "seed")
    manager = WorktreeManager(tmp_path / "worktrees")
    first = manager.create(repo, 1, 2, "main")
    second = manager.create(repo, 1, 2, "main")
    assert second.created is False
    assert second.path == first.path and second.starting_commit == first.starting_commit
    manager.remove(repo, first.path)


def test_worktree_manager_detects_orphans_and_branch_collisions(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "README").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-m", "seed")
    manager = WorktreeManager(tmp_path / "worktrees")
    first = manager.create(repo, 1, 2, "main")
    orphaned = manager.orphan_records(repo, [])
    assert [Path(item["path"]) for item in orphaned] == [first.path.resolve()]
    collision = tmp_path / "elsewhere"
    git(repo, "worktree", "add", "-b", "agents/mission-1/package-3", str(collision), "main")
    with pytest.raises(RuntimeError, match="branch collision"):
        manager.create(repo, 1, 3, "main")
    manager.remove(repo, first.path)
    git(repo, "worktree", "remove", "--force", str(collision))


def test_worktree_diagnosis_reports_health_and_dirty_state_without_file_content(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "README").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-m", "seed")
    manager = WorktreeManager(tmp_path / "worktrees")
    worktree = manager.create(repo, 3, 4, "main")

    healthy = manager.diagnose(repo, worktree.path)
    assert healthy["classification"] == "healthy"
    assert healthy["registered"] is True
    assert healthy["git_metadata_valid"] is True
    assert healthy["dirty_entries"] == 0

    (worktree.path / "sensitive-customer-name.txt").write_text("private")
    dirty = manager.diagnose(repo, worktree.path)
    assert dirty["classification"] == "dirty"
    assert dirty["dirty_entries"] == 1
    assert "sensitive-customer-name" not in str(dirty)
    manager.remove(repo, worktree.path)


def test_worktree_repair_requires_confirmation_and_never_deletes_unregistered_path(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "README").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-m", "seed")
    manager = WorktreeManager(tmp_path / "worktrees")
    unregistered = manager.root / "manual-copy"
    unregistered.mkdir(parents=True)
    marker = unregistered / "keep-me"
    marker.write_text("operator evidence")

    report = manager.diagnose(repo, unregistered)
    assert report["classification"] == "unregistered_path"
    with pytest.raises(ValueError, match="explicit confirmation"):
        manager.repair(repo, unregistered, confirm=False)
    with pytest.raises(RuntimeError, match="not contain Git worktree metadata"):
        manager.repair(repo, unregistered, confirm=True)
    assert marker.read_text() == "operator evidence"


def test_worktree_diagnosis_rejects_paths_outside_managed_root(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-b", "main")
    with pytest.raises(ValueError, match="outside the managed worktree root"):
        WorktreeManager(tmp_path / "worktrees").diagnose(repo, repo)
