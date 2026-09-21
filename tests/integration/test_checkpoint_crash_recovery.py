from __future__ import annotations

import subprocess

from orchestrator.checkpoint import CheckpointRequest, CheckpointService


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_commit_before_database_crash_recovers_existing_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    _git(repo, "init", "-b", "agents/integration-test")
    _git(repo, "config", "user.name", "Integration Test")
    _git(repo, "config", "user.email", "integration@example.invalid")

    target = repo / "feature.txt"
    target.write_text("before\n")

    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "initial")

    starting_commit = _git(repo, "rev-parse", "HEAD")

    target.write_text("after\n")

    service = CheckpointService()

    request = CheckpointRequest(
        repository=repo,
        expected_branch="agents/integration-test",
        starting_commit=starting_commit,
        approved_files=("feature.txt",),
        job_id=101,
        step_id=202,
        title="Crash recovery integration test",
    )

    # First invocation represents Git succeeding immediately before the
    # process crashes, so PostgreSQL never receives the result.
    first = service.checkpoint(request)

    assert first.success is True
    assert first.commit_sha is not None
    assert first.recovered is False

    first_sha = first.commit_sha
    count_after_first = int(
        _git(repo, "rev-list", "--count", "HEAD")
    )

    # Simulate process restart. The caller has no durable DB record of
    # first_sha and invokes the exact same checkpoint again.
    second = service.checkpoint(request)

    assert second.success is True
    assert second.recovered is True
    assert second.commit_sha == first_sha

    # Recovery must not manufacture another commit.
    assert _git(repo, "rev-parse", "HEAD") == first_sha
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == count_after_first

    marker = CheckpointService.default_marker(101, 202)
    message = _git(repo, "log", "-1", "--format=%B")

    assert marker in message


def test_commit_before_db_update_reconciles_without_duplicate_commit(
    tmp_path,
    postgres_database,
):
    from orchestrator.store import OrchestratorStore

    repo = tmp_path / "repo-db"
    repo.mkdir()

    _git(repo, "init", "-b", "agents/integration-test")
    _git(repo, "config", "user.name", "Integration Test")
    _git(repo, "config", "user.email", "integration@example.invalid")

    target = repo / "feature.txt"
    target.write_text("before\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "initial")

    starting_commit = _git(repo, "rev-parse", "HEAD")

    store = OrchestratorStore(postgres_database)
    store.migrate()

    # Seed the minimum real workflow state required by claim_checkpoint().
    with store.connect() as connection:
        job_id = connection.execute(
            """
            INSERT INTO jobs (
                goal, repository, branch, status,
                current_phase, priority
            )
            VALUES (%s, %s, %s, 'running', 'checkpoint', 0)
            RETURNING id
            """,
            (
                "Checkpoint crash recovery integration test",
                str(repo),
                "agents/integration-test",
            ),
        ).fetchone()["id"]

        step_id = connection.execute(
            """
            INSERT INTO steps (
                job_id, sequence, repository, branch,
                title, objective, acceptance_criteria,
                constraints, assigned_agent, status,
                starting_commit, files_changed, approved_files
            )
            VALUES (
                %s, 1, %s, %s,
                'Checkpoint crash recovery',
                'Prove Git/DB reconciliation after a crash.',
                '[]'::jsonb,
                '[]'::jsonb,
                'engineering-agent',
                'checkpoint',
                %s,
                '["feature.txt"]'::jsonb,
                '["feature.txt"]'::jsonb
            )
            RETURNING id
            """,
            (
                job_id,
                str(repo),
                "agents/integration-test",
                starting_commit,
            ),
        ).fetchone()["id"]

    target.write_text("after\n")

    # Real DB checkpoint claim establishes marker + durable running record.
    work = store.claim_checkpoint(
        "orch-1",
        lease_seconds=300,
        repository_lock_seconds=300,
    )

    assert work is not None
    assert work["id"] == step_id

    request = CheckpointRequest(
        repository=repo,
        expected_branch="agents/integration-test",
        starting_commit=starting_commit,
        approved_files=("feature.txt",),
        job_id=job_id,
        step_id=step_id,
        title="Checkpoint crash recovery",
        marker=work["checkpoint_marker"],
    )

    service = CheckpointService()

    # Git succeeds.
    first = service.checkpoint(request)

    assert first.success is True
    assert first.commit_sha is not None
    assert first.recovered is False

    committed_sha = first.commit_sha
    commit_count = int(_git(repo, "rev-list", "--count", "HEAD"))

    # FAILURE INJECTION:
    # Pretend the process died HERE, before record_checkpoint().
    #
    # PostgreSQL must therefore still report an in-progress checkpoint.
    with store.connect() as connection:
        checkpoint = connection.execute(
            """
            SELECT status, commit_sha
            FROM checkpoint_runs
            WHERE step_id=%s
            """,
            (step_id,),
        ).fetchone()

        step = connection.execute(
            """
            SELECT status, resulting_commit
            FROM steps
            WHERE id=%s
            """,
            (step_id,),
        ).fetchone()

    assert checkpoint["status"] == "running"
    assert checkpoint["commit_sha"] is None
    assert step["status"] == "checkpoint"
    assert step["resulting_commit"] is None

    # Process restarts and executes the same checkpoint request.
    recovered = service.checkpoint(request)

    assert recovered.success is True
    assert recovered.recovered is True
    assert recovered.commit_sha == committed_sha

    # Reconcile the recovered Git result into real PostgreSQL.
    state = store.record_checkpoint(
        work,
        "orch-1",
        recovered,
    )

    assert state == "complete"

    with store.connect() as connection:
        checkpoint = connection.execute(
            """
            SELECT status, commit_sha, completed_at
            FROM checkpoint_runs
            WHERE step_id=%s
            """,
            (step_id,),
        ).fetchone()

        step = connection.execute(
            """
            SELECT status, resulting_commit,
                   orchestrator_worker_id,
                   orchestrator_lease_expires_at
            FROM steps
            WHERE id=%s
            """,
            (step_id,),
        ).fetchone()

        job = connection.execute(
            """
            SELECT status, current_phase, current_step
            FROM jobs
            WHERE id=%s
            """,
            (job_id,),
        ).fetchone()

        events = [
            row["event_type"]
            for row in connection.execute(
                """
                SELECT event_type
                FROM events
                WHERE job_id=%s
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        ]

    assert checkpoint["status"] == "complete"
    assert checkpoint["commit_sha"] == committed_sha
    assert checkpoint["completed_at"] is not None

    assert step["status"] == "complete"
    assert step["resulting_commit"] == committed_sha
    assert step["orchestrator_worker_id"] is None
    assert step["orchestrator_lease_expires_at"] is None

    assert job["status"] == "running"
    assert job["current_phase"] == "planning"
    assert job["current_step"] is None

    assert "checkpoint_created" in events
    assert "step_completed" in events
    assert "planning_resumed" in events

    # Most important Git invariant: recovery created no duplicate commit.
    assert _git(repo, "rev-parse", "HEAD") == committed_sha
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == commit_count


def test_git_checkpoint_failure_returns_work_to_engineering(
    tmp_path,
    postgres_database,
):
    from orchestrator.store import OrchestratorStore

    repo = tmp_path / "repo-git-failure"
    repo.mkdir()

    _git(repo, "init", "-b", "agents/integration-test")
    _git(repo, "config", "user.name", "Integration Test")
    _git(repo, "config", "user.email", "integration@example.invalid")

    target = repo / "feature.txt"
    target.write_text("before\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "initial")

    starting_commit = _git(repo, "rev-parse", "HEAD")
    starting_count = int(_git(repo, "rev-list", "--count", "HEAD"))

    store = OrchestratorStore(postgres_database)
    store.migrate()

    with store.connect() as connection:
        job_id = connection.execute(
            """
            INSERT INTO jobs (
                goal, repository, branch, status,
                current_phase, priority
            )
            VALUES (%s, %s, %s, 'running', 'checkpoint', 0)
            RETURNING id
            """,
            (
                "Checkpoint Git failure integration test",
                str(repo),
                "agents/integration-test",
            ),
        ).fetchone()["id"]

        step_id = connection.execute(
            """
            INSERT INTO steps (
                job_id, sequence, repository, branch,
                title, objective, acceptance_criteria,
                constraints, assigned_agent, status,
                starting_commit, files_changed, approved_files
            )
            VALUES (
                %s, 1, %s, %s,
                'Checkpoint Git failure',
                'Prove failed checkpoint returns safely to Engineering.',
                '[]'::jsonb,
                '[]'::jsonb,
                'engineering-agent',
                'checkpoint',
                %s,
                '["feature.txt"]'::jsonb,
                '["feature.txt"]'::jsonb
            )
            RETURNING id
            """,
            (
                job_id,
                str(repo),
                "agents/integration-test",
                starting_commit,
            ),
        ).fetchone()["id"]

    target.write_text("after\n")

    work = store.claim_checkpoint(
        "orch-failure",
        lease_seconds=300,
        repository_lock_seconds=300,
    )

    assert work is not None

    # Failure injection: tell checkpointing that feature.txt was already
    # dirty before Engineering began. The service must refuse to absorb it.
    request = CheckpointRequest(
        repository=repo,
        expected_branch="agents/integration-test",
        starting_commit=starting_commit,
        approved_files=("feature.txt",),
        preexisting_files=("feature.txt",),
        job_id=job_id,
        step_id=step_id,
        title="Checkpoint Git failure",
        marker=work["checkpoint_marker"],
    )

    result = CheckpointService().checkpoint(request)

    assert result.success is False
    assert result.commit_sha is None

    state = store.record_checkpoint(
        work,
        "orch-failure",
        result,
    )

    assert state == "changes_requested"

    with store.connect() as connection:
        checkpoint = connection.execute(
            """
            SELECT status,commit_sha,error
            FROM checkpoint_runs
            WHERE step_id=%s
            """,
            (step_id,),
        ).fetchone()

        step = connection.execute(
            """
            SELECT status,resulting_commit,reviewer_feedback,
                   orchestrator_worker_id,
                   orchestrator_lease_expires_at
            FROM steps
            WHERE id=%s
            """,
            (step_id,),
        ).fetchone()

        job = connection.execute(
            """
            SELECT status,current_phase
            FROM jobs
            WHERE id=%s
            """,
            (job_id,),
        ).fetchone()

        events = [
            row["event_type"]
            for row in connection.execute(
                """
                SELECT event_type
                FROM events
                WHERE job_id=%s
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        ]

    assert checkpoint["status"] == "failed"
    assert checkpoint["commit_sha"] is None
    assert checkpoint["error"]

    assert step["status"] == "changes_requested"
    assert step["resulting_commit"] is None
    assert step["orchestrator_worker_id"] is None
    assert step["orchestrator_lease_expires_at"] is None
    assert "checkpoint_failure" in step["reviewer_feedback"]

    assert job["status"] == "running"
    assert job["current_phase"] == "engineering"

    assert "checkpoint_failed" in events

    # Failed checkpointing must not create or alter Git history.
    assert _git(repo, "rev-parse", "HEAD") == starting_commit
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == starting_count
