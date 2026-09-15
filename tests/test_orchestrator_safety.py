from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.checkpoint import CheckpointService
from orchestrator.verification import VerificationService


def git(root: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=root, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def repository(tmp_path: Path, *, branch: str = "agents/test") -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "safe.txt").write_text("before\n")
    (root / "human.txt").write_text("before\n")
    git(root, "add", "safe.txt", "human.txt")
    git(root, "commit", "-qm", "initial")
    git(root, "branch", "-M", branch)
    return root, git(root, "rev-parse", "HEAD").strip()


def test_verification_uses_explicit_instruction_command_and_blocks_secret(tmp_path: Path):
    root, _ = repository(tmp_path)
    (root / "WORKER.md").write_text(
        '## Verification\n- Unit tests: `python -c "print(\'ok\')"`\n'
    )
    git(root, "add", "WORKER.md")
    git(root, "commit", "-qm", "add verification policy")
    start = git(root, "rev-parse", "HEAD").strip()
    (root / "safe.txt").write_text("after\n")
    passed = VerificationService().verify(root, start, ["safe.txt"])
    assert passed.passed
    assert any(check.argv[0] == "python" for check in passed.commands)

    fake_key = "sk-" + "or-v1-" + "abcdefghijklmnopqrstuvwxyz"
    secret_name = "OPENROUTER_" + "API_KEY"
    (root / "safe.txt").write_text(f'{secret_name}="{fake_key}"\n')
    failed = VerificationService().verify(root, start, ["safe.txt"], config_commands=[])
    assert not failed.passed
    assert "openai_or_openrouter_key" in failed.secret_hits


def test_checkpoint_commits_only_approved_files_and_preserves_human_change(tmp_path: Path):
    root, start = repository(tmp_path)
    (root / "safe.txt").write_text("approved\n")
    (root / "human.txt").write_text("human change\n")
    service = CheckpointService()
    result = service.checkpoint(
        repository=root,
        branch="agents/test",
        starting_commit=start,
        approved_files=["safe.txt"],
        preexisting_files=["human.txt"],
        job_id=1,
        step_id=2,
        title="Update safe value",
        marker="autonomous-step:1:2:one",
    )
    assert result.success, result.message
    assert result.staged_files == ["safe.txt"]
    assert git(root, "show", "--format=", "--name-only", "HEAD").splitlines() == ["safe.txt"]
    assert git(root, "status", "--porcelain").endswith("human.txt\n")


def test_checkpoint_refuses_protected_branch_and_is_marker_idempotent(tmp_path: Path):
    protected, start = repository(tmp_path, branch="main")
    (protected / "safe.txt").write_text("after\n")
    protected_result = CheckpointService().checkpoint(
        repository=protected,
        branch="main",
        starting_commit=start,
        approved_files=["safe.txt"],
        job_id=3,
        step_id=4,
        title="Protected change",
        marker="autonomous-step:3:4:one",
    )
    assert not protected_result.success
    assert "protected branch" in protected_result.message

    root, start = repository(tmp_path / "second")
    (root / "safe.txt").write_text("after\n")
    first = CheckpointService().checkpoint(
        repository=root,
        branch="agents/test",
        starting_commit=start,
        approved_files=["safe.txt"],
        job_id=5,
        step_id=6,
        title="Idempotent change",
        marker="autonomous-step:5:6:one",
    )
    assert first.success
    repeated = CheckpointService().checkpoint(
        repository=root,
        branch="agents/test",
        starting_commit=start,
        approved_files=["safe.txt"],
        job_id=5,
        step_id=6,
        title="Idempotent change",
        marker="autonomous-step:5:6:one",
    )
    assert repeated.success and repeated.recovered
    assert repeated.commit_sha == first.commit_sha


def test_checkpoint_refuses_unreviewed_and_secret_changes(tmp_path: Path):
    root, start = repository(tmp_path)
    (root / "safe.txt").write_text("approved\n")
    (root / "unexpected.txt").write_text("not reviewed\n")
    result = CheckpointService().checkpoint(
        repository=root,
        branch="agents/test",
        starting_commit=start,
        approved_files=["safe.txt"],
        job_id=9,
        step_id=10,
        title="Do not include extras",
        marker="autonomous-step:9:10:one",
    )
    assert not result.success
    assert "unreviewed" in result.message
    assert not git(root, "log", "--format=%B", "-1").startswith("agent:")

    secret_root, secret_start = repository(tmp_path / "secret")
    fake_url = "postgresql://" + "agent:should-not-commit" + "@db.example/align"
    secret_name = "DATABASE_" + "URL"
    (secret_root / "safe.txt").write_text(f'{secret_name}="{fake_url}"\n')
    secret = CheckpointService().checkpoint(
        repository=secret_root,
        branch="agents/test",
        starting_commit=secret_start,
        approved_files=["safe.txt"],
        job_id=11,
        step_id=12,
        title="Do not commit credentials",
        marker="autonomous-step:11:12:one",
    )
    assert not secret.success
    assert "secret scan found" in secret.message
