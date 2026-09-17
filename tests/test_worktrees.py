import subprocess

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
