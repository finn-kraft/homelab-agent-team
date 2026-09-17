from __future__ import annotations

"""The deterministic workflow controller.

The specialist agents still own their reasoning.  This class does not inspect
LLM output to decide a transition; it advances exclusively from durable state
such as a reviewer verdict, a command exit status, and a Git checkpoint result.
"""

import logging
import subprocess
import threading
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import OrchestratorConfig
from .worktrees import WorktreeManager


LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    action: str
    job_id: int | None = None
    step_id: int | None = None
    detail: str = ""


class AgentOrchestrator:
    """Advance at most one durable workflow action per :meth:`once` call."""

    def __init__(
        self,
        *,
        store,
        planner,
        coder,
        reviewer,
        verifier,
        checkpoint,
        config: OrchestratorConfig,
    ):
        self.store = store
        self.planner = planner
        self.coder = coder
        self.reviewer = reviewer
        self.verifier = verifier
        self.checkpoint = checkpoint
        self.config = config
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        """Ask the foreground loop to finish its current bounded action."""
        self._stop_requested.set()

    def run(self) -> None:
        """Run until SIGTERM/SIGINT asks for a graceful safe-boundary stop."""
        self._log("orchestrator_started", worker_id=self.config.worker_id)
        heartbeat = getattr(self.store, "heartbeat_worker", None)
        while not self._stop_requested.is_set():
            try:
                if heartbeat:
                    heartbeat(self.config.worker_id, "orchestrator", "running", action="advance")
                result = self.once()
                if heartbeat:
                    heartbeat(self.config.worker_id, "orchestrator", "running",
                              job_id=result.job_id, step_id=result.step_id, action=result.action)
                if result.action == "idle":
                    self._stop_requested.wait(self.config.poll_seconds)
            except Exception:  # Database/service interruptions must not kill systemd.
                LOG.exception("orchestrator_runtime_error worker_id=%s", self.config.worker_id)
                self._stop_requested.wait(self.config.poll_seconds)
        self._log("orchestrator_stopped", worker_id=self.config.worker_id)
        if heartbeat:
            heartbeat(self.config.worker_id, "orchestrator", "stopped", action="stopped")

    def once(self) -> AdvanceResult:
        """Perform one deterministic advancement, suitable for tests and cron-like use."""
        # V2 packages are admitted into the existing durable Step pipeline.
        # Keep this additive and safe for databases that have not migrated yet.
        claim_package = getattr(self.store, "claim_work_package", None)
        if claim_package:
            try:
                package = claim_package(
                    self.config.worker_id,
                    self.config.repository_lock_seconds,
                    WorktreeManager(os.getenv("ENGINEERING_WORKTREE_ROOT", "/tmp/agent-worktrees")),
                )
                if package:
                    return AdvanceResult("package_claimed", package.get("job_id"), package.get("step_id"))
            except Exception:
                LOG.debug("v2_package_queue_unavailable", exc_info=True)

        recovered = self.store.recover_expired()
        if recovered:
            return self._handle_recovery(recovered[0])

        enforce_limit = getattr(self.store, "enforce_iteration_limit", None)
        exhausted = enforce_limit() if enforce_limit is not None else None
        if exhausted is not None:
            return AdvanceResult(
                "blocked", exhausted, detail="maximum planning iterations reached"
            )

        verification = self.store.claim_verification(
            self.config.worker_id,
            self.config.lease_seconds,
            self.config.repository_lock_seconds,
        )
        if verification:
            return self._verify(verification)

        checkpoint = self.store.claim_checkpoint(
            self.config.worker_id,
            self.config.lease_seconds,
            self.config.repository_lock_seconds,
        )
        if checkpoint:
            return self._checkpoint(checkpoint)

        step_id = self.store.next_review_step()
        if step_id is not None:
            return self._review(step_id)

        task = self.store.claim_coding(
            self.coder.worker_id,
            self.config.lease_seconds,
            self.config.repository_lock_seconds,
        )
        if task:
            return self._code(task)

        return self._plan()

    # ------------------------------------------------------------------
    # State machine actions
    # ------------------------------------------------------------------
    def _code(self, task) -> AdvanceResult:
        # The durable claim is owned by the Coder worker identity, not the
        # Orchestrator process identity.  These are commonly different under
        # systemd and must match exactly for release.
        owner = f"{self.coder.worker_id}:coder:{task.step_id}"
        started = time.monotonic()
        phase_status = "crashed"
        phase_detail = ""
        try:
            self._clear_route(self.coder)
            with self._lease_heartbeat(
                task.repository, owner, task.step_id, "coder", self.coder.worker_id
            ):
                result = self.coder.run_task(task)
            status = self.store.finish_coding_handoff(task.step_id, self.coder.worker_id)
            self._record_route(self.coder, task.job_id, task.step_id, task.attempt)
            detail = getattr(result, "summary", "")
            phase_status = str(getattr(result, "status", status))
            phase_detail = detail
            self._log("coding_finished", job_id=task.job_id, step_id=task.step_id,
                      attempt=task.attempt, status=status)
            return AdvanceResult("coding", task.job_id, task.step_id, detail)
        except Exception as exc:
            phase_detail = str(exc)
            # The Coder agent normally persists its own errors.  If it crashes
            # before doing so, the short lease plus recovery path protects us.
            self._log("coding_crashed", job_id=task.job_id, step_id=task.step_id,
                      attempt=task.attempt, error=str(exc), level=logging.ERROR)
            clean, evidence = self._repository_clean(task.repository)
            if clean and hasattr(self.store, "safely_requeue_abandoned_coding"):
                self.store.safely_requeue_abandoned_coding(task.step_id, evidence)
            elif not clean and hasattr(self.store, "block_abandoned_coding"):
                self.store.block_abandoned_coding(
                    task.step_id,
                    f"Coder crashed with unclassified repository changes. {evidence}",
                )
            return AdvanceResult("coding_crashed", task.job_id, task.step_id, str(exc))
        finally:
            self._record_phase_metric(self.coder, "coding", started, phase_status,
                                      task.job_id, task.step_id, phase_detail)
            self.store.release_repository_lock(task.repository, owner)

    def _review(self, step_id: int) -> AdvanceResult:
        started = time.monotonic()
        phase_status = "crashed"
        phase_detail = ""
        work: dict[str, Any] = {}
        try:
            self._clear_route(self.reviewer)
            decision = self.reviewer.review_once(step_id)
            status = self.store.finish_review_handoff(step_id)
            sync_package = getattr(self.store, "sync_package_for_step", None)
            if sync_package:
                package_status = {"changes_requested": "engineering", "verifying": "verifying"}.get(status)
                if package_status:
                    sync_package(step_id, package_status)
            work = self.store.step(step_id) or {}
            self._record_route(
                self.reviewer, work.get("job_id"), step_id,
                int(work.get("attempt_count") or 0),
            )
            verdict = getattr(decision, "verdict", status or "review_not_claimed")
            phase_status = str(verdict)
            phase_detail = str(verdict)
            self._log("review_finished", job_id=work.get("job_id"), step_id=step_id,
                      verdict=verdict)
            return AdvanceResult("review", work.get("job_id"), step_id, str(verdict))
        except Exception as exc:
            phase_detail = str(exc)
            work = self.store.step(step_id) or {}
            abandon = getattr(self.reviewer, "abandon", None)
            if abandon is not None:
                try:
                    abandon(step_id, str(exc))
                except Exception:
                    LOG.exception("review_cleanup_failed step_id=%s", step_id)
            self._log("review_crashed", job_id=work.get("job_id"), step_id=step_id,
                      error=str(exc), level=logging.ERROR)
            return AdvanceResult("review_crashed", work.get("job_id"), step_id, str(exc))
        finally:
            self._record_phase_metric(self.reviewer, "review", started, phase_status,
                                      work.get("job_id"), step_id, phase_detail)

    def _verify(self, work: dict[str, Any]) -> AdvanceResult:
        owner = self._lock_owner("verify", work["id"])
        started = time.monotonic()
        phase_status = "crashed"
        phase_detail = ""
        try:
            with self._lease_heartbeat(
                work["repository"], owner, work["id"], "orchestrator", self.config.worker_id
            ):
                result = self.verifier.verify(
                    repository=work["repository"],
                    starting_commit=work.get("starting_commit") or "HEAD",
                    approved_files=self._list_value(work.get("files_changed")),
                    # An unset/empty config means use the repository's explicit
                    # Verification section; it does not mean silently skip it.
                    config_commands=self.config.verification_commands or None,
                    timeout_seconds=self.config.verification_timeout_seconds,
                )
        except Exception as exc:
            result = SimpleNamespace(
                passed=False, commands=[], secret_hits=[], diff_check=None, skipped=False,
                summary=f"verification service failed safely: {exc}", service_error=True,
            )
        try:
            next_state = self.store.record_verification(work, self.config.worker_id, result)
            sync_package = getattr(self.store, "sync_package_for_step", None)
            if sync_package:
                package_status = "complete" if next_state == "complete" else ("engineering" if not getattr(result, "passed", False) else "verifying")
                sync_package(work["id"], package_status, getattr(result, "commit_sha", None))
            self._log("verification_finished", job_id=work["job_id"], step_id=work["id"],
                      passed=bool(getattr(result, "passed", False)), next_state=next_state)
            phase_status = str(next_state)
            phase_detail = getattr(result, "summary", "")
            return AdvanceResult("verification", work["job_id"], work["id"], next_state)
        finally:
            self._record_phase_metric(self.verifier, "verification", started, phase_status,
                                      work["job_id"], work["id"], phase_detail)
            self.store.release_repository_lock(work["repository"], owner)

    def _checkpoint(self, work: dict[str, Any]) -> AdvanceResult:
        owner = self._lock_owner("checkpoint", work["id"])
        started = time.monotonic()
        phase_status = "crashed"
        phase_detail = ""
        try:
            if not self.config.auto_commit:
                result = SimpleNamespace(
                    success=False, retryable=False,
                    error="AUTO_COMMIT is disabled; human checkpoint approval is required.",
                )
            else:
                with self._lease_heartbeat(
                    work["repository"], owner, work["id"], "orchestrator",
                    self.config.worker_id,
                ):
                    result = self.checkpoint.checkpoint(
                        repository=work["repository"],
                        branch=work["branch"],
                        protected_branches=self.config.protected_branches,
                        approved_files=self._list_value(work.get("approved_files")),
                        preexisting_files=self._preexisting_files(work["id"]),
                        starting_commit=work.get("starting_commit"),
                        step_id=work["id"],
                        job_id=work["job_id"],
                        title=work["title"],
                        marker=work["checkpoint_marker"],
                        auto_push=self.config.auto_push,
                    )
        except Exception as exc:
            result = SimpleNamespace(success=False, retryable=False, error=f"checkpoint service failed: {exc}")
        try:
            next_state = self.store.record_checkpoint(work, self.config.worker_id, result)
            sync_package = getattr(self.store, "sync_package_for_step", None)
            if sync_package and next_state == "complete":
                sync_package(work["id"], "complete", getattr(result, "commit_sha", None))
            self._log("checkpoint_finished", job_id=work["job_id"], step_id=work["id"],
                      commit_sha=getattr(result, "commit_sha", None), next_state=next_state)
            phase_status = str(next_state)
            phase_detail = getattr(result, "error", None) or ""
            return AdvanceResult("checkpoint", work["job_id"], work["id"], next_state)
        finally:
            self._record_phase_metric(self.checkpoint, "checkpoint", started, phase_status,
                                      work["job_id"], work["id"], phase_detail)
            self.store.release_repository_lock(work["repository"], owner)

    def _plan(self) -> AdvanceResult:
        started = time.monotonic()
        phase_status = "crashed"
        phase_detail = ""
        try:
            self._clear_route(self.planner)
            decision = self.planner.plan_once()
            if decision is None:
                phase_status = "idle"
        except Exception as exc:
            phase_detail = str(exc)
            self._log("planning_crashed", error=str(exc), level=logging.ERROR)
            return AdvanceResult("planning_crashed", detail=str(exc))
        finally:
            job_id = getattr(self.planner, "last_job_id", None)
            if "decision" in locals() and decision is not None:
                phase_status = str(getattr(decision, "decision", "finished"))
                phase_detail = str(getattr(decision, "reasoning_summary", ""))
            self._record_phase_metric(self.planner, "planning", started, phase_status,
                                      job_id, None, phase_detail)
        if decision is None:
            phase_status = "idle"
            return AdvanceResult("idle")
        job_id = getattr(self.planner, "last_job_id", None)
        self._record_route(self.planner, job_id, None, None)
        self._log("planning_finished", job_id=job_id, decision=getattr(decision, "decision", None))
        return AdvanceResult("planning", job_id, detail=str(getattr(decision, "decision", "")))

    # ------------------------------------------------------------------
    # Crash recovery and observability
    # ------------------------------------------------------------------
    def _handle_recovery(self, stale: dict[str, Any]) -> AdvanceResult:
        step_id = int(stale["id"])
        status = stale["status"]
        if status != "running":
            # Verification has no external mutation and checkpoint has a
            # durable marker, so both are safe to re-claim on the next tick.
            return AdvanceResult("lease_recovered", stale["job_id"], step_id, status)
        clean, evidence = self._repository_clean(stale["repository"])
        if clean:
            self.store.safely_requeue_abandoned_coding(step_id, evidence)
            return AdvanceResult("coding_requeued", stale["job_id"], step_id, evidence)
        self.store.block_abandoned_coding(
            step_id,
            f"Coder lease expired with unclassified repository changes. {evidence}",
        )
        return AdvanceResult("coding_blocked", stale["job_id"], step_id, evidence)

    @staticmethod
    def _repository_clean(repository: str) -> tuple[bool, str]:
        root = Path(repository)
        try:
            done = subprocess.run(
                ["git", "status", "--porcelain=v1", "-uall"], cwd=root,
                text=True, capture_output=True, timeout=30, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not inspect repository safely: {exc}"
        if done.returncode != 0:
            return False, f"git status failed: {done.stderr[:1000]}"
        if not done.stdout.strip():
            return True, "working tree is clean after expired Coder lease"
        return False, f"working tree has changes: {done.stdout[:4000]}"

    def _preexisting_files(self, step_id: int) -> list[str]:
        """Read Coder's recorded baseline without guessing from current Git state."""
        # Store implementations may expose a specialised helper.  Keeping this
        # optional lets test doubles stay small while retaining safety in prod.
        getter = getattr(self.store, "preexisting_files", None)
        if getter is None:
            return []
        return self._list_value(getter(step_id))

    def _record_route(self, agent: Any, job_id: int | None, step_id: int | None,
                      attempt: int | None) -> None:
        route = getattr(getattr(agent, "router", None), "last_route", None)
        if route is None:
            return
        self.store.record_model_invocation(
            job_id=job_id,
            step_id=step_id,
            caller_agent=getattr(route, "caller_agent", type(agent).__name__),
            provider=getattr(route, "provider", "unknown"),
            model=getattr(route, "model", None),
            route_reason=getattr(route, "reason", None),
            attempt=attempt if attempt is not None else getattr(route, "attempt", None),
            latency_seconds=getattr(route, "latency_seconds", None),
            usage=getattr(route, "usage", None),
            estimated_cloud_cost=getattr(route, "estimated_cloud_cost", None),
            fallback=bool(getattr(route, "fallback", False)),
        )

    def _record_phase_metric(self, agent: Any, phase: str, started: float,
                             status: str, job_id: int | None = None,
                             step_id: int | None = None, detail: str = "") -> None:
        duration = time.monotonic() - started
        prompt_chars = int(getattr(agent, "last_prompt_chars", 0) or 0)
        route = getattr(getattr(agent, "router", None), "last_route", None)
        provider = getattr(route, "provider", None)
        model = getattr(route, "model", None) or getattr(agent, "last_model", None)
        recorder = getattr(self.store, "record_phase_metric", None)
        if recorder is not None:
            try:
                recorder(
                    job_id=job_id,
                    step_id=step_id,
                    phase=phase,
                    status=status,
                    duration_seconds=duration,
                    prompt_chars=prompt_chars,
                    provider=provider,
                    model=model,
                    detail={"detail": detail[:4_000]} if detail else {},
                )
            except Exception:
                LOG.exception("phase_metric_record_failed phase=%s job_id=%s step_id=%s",
                              phase, job_id, step_id)
        self._log("phase_finished", phase=phase, status=status, job_id=job_id,
                  step_id=step_id, duration_seconds=round(duration, 3),
                  prompt_chars=prompt_chars, provider=provider, model=model)

    @staticmethod
    def _clear_route(agent: Any) -> None:
        router = getattr(agent, "router", None)
        if router is not None and hasattr(router, "last_route"):
            router.last_route = None

    def _lock_owner(self, phase: str, step_id: int) -> str:
        return f"{self.config.worker_id}:{phase}:{step_id}"

    @contextmanager
    def _lease_heartbeat(
        self,
        repository: str,
        owner: str,
        step_id: int,
        lease_kind: str,
        lease_worker_id: str,
    ):
        """Renew durable ownership while a bounded action is still executing."""
        stop = threading.Event()
        interval = max(
            1.0,
            min(self.config.lease_seconds, self.config.repository_lock_seconds) / 3,
        )

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    repository_ok = self.store.heartbeat_repository_lock(
                        repository, owner, self.config.repository_lock_seconds
                    )
                    if lease_kind == "coder":
                        work_ok = self.store.heartbeat_coding(
                            step_id, lease_worker_id, self.config.lease_seconds
                        )
                    else:
                        work_ok = self.store.heartbeat_orchestration(
                            step_id, lease_worker_id, self.config.lease_seconds
                        )
                    if not repository_ok or not work_ok:
                        self._log(
                            "lease_heartbeat_lost", level=logging.ERROR,
                            step_id=step_id, owner=owner, phase=lease_kind,
                        )
                        return
                except Exception as exc:
                    self._log(
                        "lease_heartbeat_failed", level=logging.ERROR,
                        step_id=step_id, owner=owner, phase=lease_kind, error=str(exc),
                    )

        thread = threading.Thread(
            target=heartbeat,
            name=f"orchestrator-lease-{step_id}-{lease_kind}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=min(interval, 2.0))

    @staticmethod
    def _list_value(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, tuple):
            return [str(item) for item in value]
        return []

    @staticmethod
    def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
        structured = " ".join(f"{key}={value!s}" for key, value in sorted(fields.items()))
        LOG.log(level, "%s %s", event, structured)
