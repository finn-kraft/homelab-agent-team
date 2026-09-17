"""Focused safety tests for deterministic verification and Git checkpoints."""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.checkpoint import CheckpointService
from orchestrator.verification import VerificationService


def _run(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        list(argv), cwd=root, check=True, text=True, capture_output=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, branch: str = "agents/autonomous-align") -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _run(root, "git", "init", "-q", "-b", branch)
    # The controlled checkpoint deliberately does not invent an author; the
    # repository/operator owns commit identity.  Supply one in this isolated
    # test repository just as a real worker checkout would.
    _run(root, "git", "config", "user.name", "Test Worker")
    _run(root, "git", "config", "user.email", "worker@example.invalid")
    (root / "feature.py").write_text("value = 1\n")
    _run(root, "git", "add", "feature.py")
    _run(root, "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    return root, _run(root, "git", "rev-parse", "HEAD")


def test_checkpoint_stages_only_approved_files_and_preserves_human_work(tmp_path):
    root, start = _repository(tmp_path)
    (root / "feature.py").write_text("value = 2\n")
    (root / "human-notes.txt").write_text("do not include this\n")

    result = CheckpointService().checkpoint(
        repository=root,
        branch="agents/autonomous-align",
        starting_commit=start,
        approved_files=["feature.py"],
        preexisting_files=["human-notes.txt"],
        job_id=1,
        step_id=7,
        title="Update the tested feature",
    )

    assert result.success, result.message
    assert result.staged_files == ["feature.py"]
    assert _run(root, "git", "show", "--format=", "--name-only", "HEAD") == "feature.py"
    assert "?? human-notes.txt" in _run(root, "git", "status", "--porcelain")


def test_checkpoint_refuses_protected_branch(tmp_path):
    root, start = _repository(tmp_path, branch="main")
    (root / "feature.py").write_text("value = 2\n")

    result = CheckpointService().checkpoint(
        repository=root,
        branch="main",
        starting_commit=start,
        approved_files=["feature.py"],
        job_id=1,
        step_id=7,
        title="Unsafe direct main change",
    )

    assert not result.success
    assert "protected branch" in result.message
    assert _run(root, "git", "rev-parse", "HEAD") == start


def test_secret_scan_prevents_checkpoint_and_verification(tmp_path):
    root, start = _repository(tmp_path)
    fake_key = "sk-" + "or-v1-" + "this-should-never-be-committed"
    secret_name = "API_" + "KEY"
    (root / "feature.py").write_text(f"{secret_name}='{fake_key}'\n")

    verification = VerificationService().verify(root, start, ["feature.py"], commands=[])
    checkpoint = CheckpointService().checkpoint(
        repository=root,
        branch="agents/autonomous-align",
        starting_commit=start,
        approved_files=["feature.py"],
        job_id=1,
        step_id=7,
        title="Attempt to commit a secret",
    )

    assert not verification.passed
    assert verification.secret_hits
    assert not checkpoint.success
    assert "secret scan" in checkpoint.message
    assert _run(root, "git", "rev-parse", "HEAD") == start


def test_checkpoint_marker_recovers_after_commit_before_persistence(tmp_path):
    root, start = _repository(tmp_path)
    (root / "feature.py").write_text("value = 2\n")
    checkpoint = CheckpointService()
    marker = "autonomous-step:1:7:recovery"
    first = checkpoint.checkpoint(
        repository=root,
        branch="agents/autonomous-align",
        starting_commit=start,
        approved_files=["feature.py"],
        job_id=1,
        step_id=7,
        title="Checkpoint with recovery marker",
        marker=marker,
    )
    second = checkpoint.checkpoint(
        repository=root,
        branch="agents/autonomous-align",
        starting_commit=start,
        approved_files=["feature.py"],
        job_id=1,
        step_id=7,
        title="Checkpoint with recovery marker",
        marker=marker,
    )

    assert first.success
    assert second.success and second.recovered
    assert second.commit_sha == first.commit_sha


def test_checkpoint_staging_failure_is_safe_and_recoverable(tmp_path):
    root, start = _repository(tmp_path)
    (root / "feature.py").write_text("value = 2\n")

    class FailingStage(CheckpointService):
        def _run(self, root, argv, *, redact_output=True):
            if tuple(argv[:2]) == ("git", "add"):
                return subprocess.CompletedProcess(list(argv), 1, "", "injected staging failure")
            return super()._run(root, argv, redact_output=redact_output)

    result = FailingStage().checkpoint(
        repository=root, branch="agents/autonomous-align", starting_commit=start,
        approved_files=["feature.py"], job_id=1, step_id=8, title="staging fault",
    )
    assert not result.success and result.failure_code == "checkpoint_refused"
    assert _run(root, "git", "rev-parse", "HEAD") == start
    assert not _run(root, "git", "diff", "--cached", "--name-only")


def test_checkpoint_push_failure_keeps_local_commit_and_evidence(tmp_path):
    root, start = _repository(tmp_path)
    (root / "feature.py").write_text("value = 2\n")

    class FailingPush(CheckpointService):
        def _run(self, root, argv, *, redact_output=True):
            if tuple(argv) == ("git", "push"):
                return subprocess.CompletedProcess(list(argv), 1, "", "injected push failure")
            return super()._run(root, argv, redact_output=redact_output)

    result = FailingPush().checkpoint(
        repository=root, branch="agents/autonomous-align", starting_commit=start,
        approved_files=["feature.py"], job_id=1, step_id=9, title="push fault",
        auto_push=True,
    )
    assert result.success
    assert result.details["push_attempted"] is True
    assert result.details["push_exit_code"] == 1
    assert "push failed" in result.message
    assert _run(root, "git", "rev-parse", "HEAD") != start
