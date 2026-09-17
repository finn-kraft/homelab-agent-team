from __future__ import annotations

import subprocess
from types import SimpleNamespace

from orchestrator.checkpoint import CheckpointRequest, CheckpointService
from orchestrator.store import OrchestratorStore
from reviewer_agent.decision import parse_review
from reviewer_agent.store import ReviewerStore


CRITERION = "The package implementation is complete."


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def approved():
    return parse_review(
        """{
          "verdict": "approved",
          "summary": "Implementation satisfies the package.",
          "blocking_issues": [],
          "non_blocking_suggestions": [],
          "acceptance_criteria": [{
            "criterion": "The package implementation is complete.",
            "status": "satisfied",
            "evidence": "Reviewed implementation satisfies the criterion."
          }],
          "risk": "low",
          "confidence": "high",
          "recommended_next_state": "verification"
        }""",
        [CRITERION],
    )


def revision():
    return parse_review(
        """{
          "verdict": "changes_requested",
          "summary": "One deterministic correction is required.",
          "blocking_issues": [{
            "severity": "medium",
            "category": "correctness",
            "file": "package1.txt",
            "line": 1,
            "problem": "First revision deliberately requires correction.",
            "evidence": "Acceptance fixture injects one review rejection.",
            "requested_change": "Revise package 1 and submit it again."
          }],
          "non_blocking_suggestions": [],
          "acceptance_criteria": [{
            "criterion": "The package implementation is complete.",
            "status": "not_satisfied",
            "evidence": "The deliberate first review requires revision."
          }],
          "risk": "medium",
          "confidence": "high",
          "recommended_next_state": "engineering_revision"
        }""",
        [CRITERION],
    )


def evidence():
    return {
        "checks": {"fixture": {"exit_code": 0}},
        "diff_sha256": "integration-fixture",
    }


def review_meta():
    return {
        "model": "deterministic-integration-test",
        "provider": "local",
        "latency": 0.0,
        "usage": {},
    }


def verification_pass():
    return SimpleNamespace(
        passed=True,
        service_error=False,
        summary="integration verification passed",
        secret_hits=[],
        diff_check=0,
        skipped=False,
        commands=[],
    )


def test_three_real_packages_revision_verification_and_checkpoint(
    tmp_path,
    postgres_database,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    git(repo, "init", "-b", "agents/integration-test")
    git(repo, "config", "user.name", "Integration Test")
    git(repo, "config", "user.email", "integration@example.invalid")

    (repo / "README.md").write_text("integration fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")

    store = OrchestratorStore(postgres_database)
    store.migrate()
    reviewer = ReviewerStore(postgres_database)
    checkpoint = CheckpointService()

    mission_id = store.create_mission(
        "Complete three integration packages",
        str(repo),
        "agents/integration-test",
    )

    package_ids = [
        store.create_work_package(
            mission_id,
            f"Implement package {number}.",
            str(repo),
            "agents/integration-test",
            [CRITERION],
        )
        for number in range(1, 4)
    ]

    commits = []
    review_counts = {}

    for number, package_id in enumerate(package_ids, start=1):
        claimed = store.claim_work_package(
            f"engineer-{number}",
            lease_seconds=300,
        )

        assert claimed is not None
        assert claimed["id"] == package_id

        step_id = claimed["step_id"]
        job_id = claimed["job_id"]

        task = store.claim_engineering_for_step(
            step_id,
            f"engineer-{number}",
            lease_seconds=300,
            repository_lock_seconds=300,
        )

        assert task is not None

        starting_commit = git(repo, "rev-parse", "HEAD")

        with store.connect() as connection:
            connection.execute(
                """
                UPDATE steps
                SET starting_commit=%s
                WHERE id=%s
                """,
                (starting_commit, step_id),
            )

        target = repo / f"package{number}.txt"
        target.write_text(f"package {number} implementation\n")

        # Engineering -> Review through the production handoff.
        with store.connect() as connection:
            connection.execute(
                """
                UPDATE steps
                SET status='review',
                    files_changed=%s::jsonb,
                    worker_id=NULL,
                    lease_expires_at=NULL,
                    updated_at=now()
                WHERE id=%s
                """,
                (f'["package{number}.txt"]', step_id),
            )

        assert store.finish_engineering_handoff(
            step_id,
            f"engineer-{number}",
        ) == "review"

        store.release_repository_lock(
            str(repo),
            f"engineer-{number}:engineering:{step_id}",
        )

        assert store.next_review_step() == step_id

        item = reviewer.claim(
            "reviewer-1",
            300,
            step_id,
        )

        assert item is not None

        review_counts[step_id] = review_counts.get(step_id, 0) + 1

        if number == 1:
            reviewer.complete(
                item,
                "reviewer-1",
                revision(),
                review_meta(),
                evidence(),
            )

            assert store.finish_review_handoff(
                step_id
            ) == "changes_requested"

            with store.connect() as connection:
                state = connection.execute(
                    "SELECT status FROM steps WHERE id=%s",
                    (step_id,),
                ).fetchone()["status"]

            assert state == "changes_requested"

            # Same Engineering package/session receives the revision.
            revised_task = store.claim_engineering_for_step(
                step_id,
                "engineer-1",
                lease_seconds=300,
                repository_lock_seconds=300,
            )

            assert revised_task is not None

            target.write_text(
                "package 1 implementation\nreview correction applied\n"
            )

            with store.connect() as connection:
                connection.execute(
                    """
                    UPDATE steps
                    SET status='review',
                        worker_id=NULL,
                        lease_expires_at=NULL,
                        updated_at=now()
                    WHERE id=%s
                    """,
                    (step_id,),
                )

            assert store.finish_engineering_handoff(
                step_id,
                "engineer-1",
            ) == "review"

            store.release_repository_lock(
                str(repo),
                f"engineer-1:engineering:{step_id}",
            )

            assert store.next_review_step() == step_id

            item = reviewer.claim(
                "reviewer-1",
                300,
                step_id,
            )

            assert item is not None
            review_counts[step_id] += 1

        reviewer.complete(
            item,
            "reviewer-1",
            approved(),
            review_meta(),
            evidence(),
        )

        assert store.finish_review_handoff(
            step_id
        ) == "verification"

        # Reviewer approval -> real Verification persistence.
        verification_work = store.claim_verification(
            "orch-verify",
            lease_seconds=300,
            repository_lock_seconds=300,
        )

        assert verification_work is not None
        assert verification_work["id"] == step_id

        next_state = store.record_verification(
            verification_work,
            "orch-verify",
            verification_pass(),
        )

        assert next_state == "checkpoint"

        # The coordinator releases the Verification repository lock
        # before handing the same repository to Checkpoint.
        store.release_repository_lock(
            str(repo),
            f"orch-verify:verify:{step_id}",
        )

        # Verification -> real checkpoint claim and Git commit.
        checkpoint_work = store.claim_checkpoint(
            "orch-checkpoint",
            lease_seconds=300,
            repository_lock_seconds=300,
        )

        assert checkpoint_work is not None
        assert checkpoint_work["id"] == step_id

        result = checkpoint.checkpoint(
            CheckpointRequest(
                repository=repo,
                expected_branch="agents/integration-test",
                starting_commit=starting_commit,
                approved_files=(f"package{number}.txt",),
                job_id=job_id,
                step_id=step_id,
                title=f"Package {number}",
                marker=checkpoint_work["checkpoint_marker"],
            )
        )

        assert result.success is True, result.error
        assert result.commit_sha is not None

        state = store.record_checkpoint(
            checkpoint_work,
            "orch-checkpoint",
            result,
        )

        assert state == "complete"

        # The coordinator releases the Checkpoint repository lock
        # before another package may use this repository.
        store.release_repository_lock(
            str(repo),
            f"orch-checkpoint:checkpoint:{step_id}",
        )

        store.sync_package_for_step(
            step_id,
            "complete",
            result.commit_sha,
        )

        commits.append(result.commit_sha)

    # Mission/package truth.
    packages = store.list_work_packages(mission_id)

    assert len(packages) == 3
    assert [p["status"] for p in packages] == [
        "complete",
        "complete",
        "complete",
    ]

    assert len(set(commits)) == 3

    # Package 1 was reviewed twice; packages 2 and 3 once each.
    assert sorted(review_counts.values()) == [1, 1, 2]

    with store.connect() as connection:
        steps = connection.execute(
            """
            SELECT status,resulting_commit,
                   worker_id,lease_expires_at,
                   reviewer_worker_id,review_lease_expires_at,
                   orchestrator_worker_id,orchestrator_lease_expires_at
            FROM steps
            ORDER BY id
            """
        ).fetchall()

        reviews = connection.execute(
            """
            SELECT verdict,count(*) AS n
            FROM reviews
            GROUP BY verdict
            ORDER BY verdict
            """
        ).fetchall()

        verification_count = connection.execute(
            """
            SELECT count(*) AS n
            FROM verification_runs
            WHERE status='passed'
            """
        ).fetchone()["n"]

        checkpoint_count = connection.execute(
            """
            SELECT count(*) AS n
            FROM checkpoint_runs
            WHERE status='complete'
            """
        ).fetchone()["n"]

    assert len(steps) == 3

    for step in steps:
        assert step["status"] == "complete"
        assert step["resulting_commit"] in commits
        assert step["worker_id"] is None
        assert step["lease_expires_at"] is None
        assert step["reviewer_worker_id"] is None
        assert step["review_lease_expires_at"] is None
        assert step["orchestrator_worker_id"] is None
        assert step["orchestrator_lease_expires_at"] is None

    verdict_counts = {
        row["verdict"]: row["n"]
        for row in reviews
    }

    assert verdict_counts == {
        "approved": 3,
        "changes_requested": 1,
    }

    assert verification_count == 3
    assert checkpoint_count == 3

    # Git truth must agree with PostgreSQL truth.
    assert git(repo, "rev-parse", "HEAD") == commits[-1]

    history = git(repo, "log", "--format=%s", "-3").splitlines()

    assert history == [
        "agent: Package 3",
        "agent: Package 2",
        "agent: Package 1",
    ]
