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
