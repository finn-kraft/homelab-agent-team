from __future__ import annotations

from orchestrator.store import OrchestratorStore


def _seed_metric_mission(store: OrchestratorStore) -> int:
    mission_id = store.create_mission(
        "Measure authoritative mission evidence",
        "/tmp/integration-repository",
        "agents/integration-test",
    )
    package_ids = [
        store.create_work_package(
            mission_id,
            objective,
            "/tmp/integration-repository",
            "agents/integration-test",
            ["Durable evidence exists"],
        )
        for objective in ("Integrated package", "Pending integration", "Cancelled package")
    ]
    packages = store.list_work_packages(mission_id)

    with store.connect() as connection:
        connection.execute(
            "UPDATE missions SET created_at=now()-interval '10 minutes' WHERE id=%s",
            (mission_id,),
        )
        for index, package in enumerate(packages[:2], 1):
            step_id = package["step_id"]
            job_id = package["job_id"]
            connection.execute(
                """UPDATE work_packages SET status='complete',resulting_commit=%s,
                   updated_at=now()-(%s*interval '1 second') WHERE id=%s""",
                (f"commit-{index}", 60 * index, package["id"]),
            )
            connection.execute(
                """UPDATE steps SET status='complete',resulting_commit=%s,
                   completed_at=now()-(%s*interval '1 second') WHERE id=%s""",
                (f"commit-{index}", 60 * index, step_id),
            )
            if index == 1:
                connection.execute(
                    """INSERT INTO reviews
                       (job_id,step_id,review_attempt,reviewer_worker_id,verdict,completed_at)
                       VALUES(%s,%s,1,'reviewer-1','changes_requested',now()-interval '8 minutes')""",
                    (job_id, step_id),
                )
                connection.execute(
                    """INSERT INTO reviews
                       (job_id,step_id,review_attempt,reviewer_worker_id,verdict,completed_at)
                       VALUES(%s,%s,2,'reviewer-1','approved',now()-interval '7 minutes')""",
                    (job_id, step_id),
                )
                connection.execute(
                    """INSERT INTO verification_runs
                       (job_id,step_id,attempt,worker_id,status,commands,details,completed_at)
                       VALUES(%s,%s,1,'orchestrator-1','failed','[]','{}',now()-interval '6 minutes')""",
                    (job_id, step_id),
                )
                connection.execute(
                    """INSERT INTO verification_runs
                       (job_id,step_id,attempt,worker_id,status,commands,details,completed_at)
                       VALUES(%s,%s,2,'orchestrator-1','passed','[]','{}',now()-interval '5 minutes')""",
                    (job_id, step_id),
                )
            else:
                connection.execute(
                    """INSERT INTO reviews
                       (job_id,step_id,review_attempt,reviewer_worker_id,verdict,completed_at)
                       VALUES(%s,%s,1,'reviewer-1','approved',now()-interval '5 minutes')""",
                    (job_id, step_id),
                )
                connection.execute(
                    """INSERT INTO verification_runs
                       (job_id,step_id,attempt,worker_id,status,commands,details,completed_at)
                       VALUES(%s,%s,1,'orchestrator-1','passed','[]','{}',now()-interval '4 minutes')""",
                    (job_id, step_id),
                )
            connection.execute(
                """INSERT INTO checkpoint_runs
                   (step_id,job_id,marker,status,commit_sha,completed_at)
                   VALUES(%s,%s,%s,'complete',%s,now()-interval '3 minutes')""",
                (step_id, job_id, f"metric-marker-{index}", f"commit-{index}"),
            )

        connection.execute(
            """UPDATE work_packages SET status='cancelled' WHERE id=%s""",
            (package_ids[2],),
        )
        connection.execute(
            """UPDATE jobs SET status='cancelled' WHERE id=%s""",
            (packages[2]["job_id"],),
        )

    store.upsert_integration(
        mission_id=mission_id,
        package_id=package_ids[0],
        source_branch="agents/package-1",
        target_branch="agents/integration-test",
        status="complete",
        commit_sha="integrated-commit-1",
    )

    first, second = packages[:2]
    store.record_phase_metric(
        job_id=first["job_id"], step_id=first["step_id"], phase="engineering",
        status="complete", duration_seconds=10, prompt_chars=1000, prompt_tokens=250,
    )
    store.record_phase_metric(
        job_id=second["job_id"], step_id=second["step_id"], phase="engineering",
        status="complete", duration_seconds=20, prompt_chars=2000, prompt_tokens=500,
    )
    store.record_phase_metric(
        job_id=first["job_id"], step_id=first["step_id"], phase="review",
        status="approved", duration_seconds=4, prompt_chars=500, prompt_tokens=125,
    )
    store.record_model_invocation(
        job_id=first["job_id"], step_id=first["step_id"], caller_agent="engineering-agent",
        provider="ollama", model="qwen", route_reason="local", attempt=1,
        latency_seconds=2, usage={"input_tokens": 100, "output_tokens": 20},
    )
    store.record_model_invocation(
        job_id=second["job_id"], step_id=second["step_id"], caller_agent="reviewer-agent",
        provider="openrouter", model="sonnet", route_reason="quality", attempt=1,
        latency_seconds=3, usage={"prompt_tokens": 200, "completion_tokens": 40},
        estimated_cloud_cost=0.25, fallback=True,
    )
    return mission_id


def test_mission_metrics_are_derived_from_durable_evidence(postgres_database):
    store = OrchestratorStore(postgres_database)
    store.migrate()
    mission_id = _seed_metric_mission(store)

    metrics = store.mission_metrics(mission_id)

    assert metrics["package_progress"] == {
        "total": 3,
        "denominator": 2,
        "evidence_complete": 1,
        "execution_complete": 2,
        "cancelled": 1,
        "blocked_or_failed": 0,
        "active_or_ready": 0,
        "percent": 50.0,
        "execution_percent": 100.0,
    }
    assert metrics["quality"]["review_attempts"] == 3
    assert metrics["quality"]["reviewed_packages"] == 2
    assert metrics["quality"]["approved_packages"] == 2
    assert metrics["quality"]["changes_requested"] == 1
    assert metrics["quality"]["first_pass_approval_percent"] == 50.0
    assert metrics["quality"]["revision_cycles"] == 2
    assert metrics["verification"]["runs"] == 3
    assert metrics["verification"]["passed"] == 2
    assert metrics["verification"]["failed"] == 1
    assert metrics["verification"]["first_pass_percent"] == 50.0
    assert metrics["phases"]["engineering"]["runs"] == 2
    assert metrics["phases"]["engineering"]["average_seconds"] == 15.0
    assert metrics["phases"]["engineering"]["total_seconds"] == 30.0
    assert metrics["models"]["calls"] == 2
    assert metrics["models"]["local_calls"] == 1
    assert metrics["models"]["cloud_calls"] == 1
    assert metrics["models"]["input_tokens"] == 300
    assert metrics["models"]["output_tokens"] == 60
    assert metrics["models"]["estimated_cloud_cost"] == 0.25
    assert metrics["latency"]["elapsed_seconds"] >= 590

    restarted = OrchestratorStore(postgres_database).mission_metrics(mission_id)
    for key in ("package_progress", "quality", "verification", "phases", "models"):
        assert restarted[key] == metrics[key]
