from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .commands import CommandRejected, CommandRunner
from .db import Store
from .git import GitError, GitRepository
from .llm import BackendError, Router
from .models import AgentResult, Status, Task, WorkPackage
from agent_core.prompt_budget import (
    DEFAULT_PROMPT_CHARS,
    bounded_json,
    bounded_messages,
    message_chars,
)


# This module is the temporary V1 import location for the V2 EngineeringAgent.
# New deployments use engineering_agent.cli and engineering_agent imports;
# CoderAgent remains an explicit compatibility alias at the end of the file.
from .workspace import Workspace, WorkspaceViolation


SYSTEM_PROMPT = """You are an Engineering Worker, the implementation owner of one Work Package.
Inspect the repository and project instructions first, form and revise your implementation
plan internally, then implement, test, debug, and prepare a reviewable candidate. The
reviewer owns approval, but reviewer feedback returns to this same package session.
Work only within the package objective and constraints.
Repository paths are relative to the repository root. The `repository` value is an
absolute label, not a file path. Use the provided `repository_files` inventory or
`inspect` with `kind:"tree"` before choosing a path; never guess a filename.
Return exactly one JSON object per turn, with one action:
{"action":"read","path":"relative/path"}
{"action":"write","path":"relative/path","content":"complete file content"}
{"action":"delete","path":"relative/path","justification":"specific reason"}
{"action":"run","argv":["pytest","-q"],"timeout":300}
{"action":"inspect","kind":"status|diff|instructions|tree"}
{"action":"finish","summary":"...","verification":["..."]}
{"action":"blocked","reason":"..."}
Never claim a command passed unless its recorded exit code is zero. Do not commit, push,
change branches, access secrets, or expand the assignment. Prefer small, reviewable edits.
"""


class EngineeringAgent:
    def __init__(self, store: Store, router: Router, worker_id: str,
                 allowed_roots: list[str], max_turns: int | None = None, max_attempts: int = 4,
                 max_prompt_chars: int = DEFAULT_PROMPT_CHARS,
                 max_stagnation_episodes: int = 6):
        self.store, self.router, self.worker_id = store, router, worker_id
        self.allowed_roots = allowed_roots
        # An overall turn cutoff is retained only for explicit compatibility
        # callers. Production Engineering uses progress/stagnation detection;
        # a productive package is not interrupted just because it crossed 30
        # model/tool turns.
        self.max_turns = None if max_turns is None or int(max_turns) <= 0 else int(max_turns)
        self.max_attempts = max_attempts
        self.max_stagnation_episodes = max(1, int(max_stagnation_episodes))
        self.max_prompt_chars = max(1_024, int(max_prompt_chars))
        self.last_prompt_chars = 0

    def run_work_package(self, package: WorkPackage | Task, step_id: int | None = None,
                         attempt: int = 0) -> AgentResult:
        """Run one Work Package using the preserved workspace/tooling loop.

        ``Task`` remains the V1-compatible transport while V2 introduces a
        package-owned execution boundary. Reviewer feedback is carried on the
        same object and the package worktree remains the source of truth across
        retries and restarts.
        """
        if isinstance(package, WorkPackage):
            if step_id is None:
                raise ValueError("a V2 WorkPackage requires its durable step_id")
            package = package.as_task(step_id, attempt)
        return self.run_task(package)

    # V2's public name is ``run_work_package``. Keep the earlier method name
    # for callers that adopted the first package transport increment.
    def run_package(self, package: WorkPackage | Task, step_id: int | None = None,
                    attempt: int = 0) -> AgentResult:
        return self.run_work_package(package, step_id, attempt)

    @staticmethod
    def _parse_action(text: str) -> dict[str, Any]:
        try:
            action = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("model response was not valid JSON") from exc
        if not isinstance(action, dict) or "action" not in action:
            raise ValueError("model response has no action")
        return action

    @staticmethod
    def _redact(value: str) -> str:
        patterns = [
            r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,]+",
            r"sk-[A-Za-z0-9_-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
        ]
        for pattern in patterns:
            value = re.sub(pattern, "[REDACTED]", value)
        return value[:30_000]

    def run_task(self, task: Task) -> AgentResult:
        self.last_prompt_chars = 0
        if task.attempt > self.max_attempts:
            result = AgentResult(
                Status.FAILED,
                "retry limit reached; return step for replanning",
                blocker="maximum engineering attempts exceeded",
            )
            self.store.update_step(
                task.step_id,
                self.worker_id,
                result.status,
                blocker=result.blocker,
            )
            return result
        try:
            workspace = Workspace(task.repository, self.allowed_roots)
            runner = CommandRunner(workspace)
            git = GitRepository(runner)
            starting_commit = git.head()
            session_id = None
            session_baseline: dict[str, Any] = {}
            session_state: dict[str, Any] = {}
            resume_session = getattr(self.store, "resume_engineering_session", None)
            if resume_session:
                session_id = resume_session(task.job_id, task.step_id)
            load_session = getattr(self.store, "engineering_session_state", None)
            if session_id is not None and load_session:
                session_state = load_session(task.job_id, task.step_id) or {}
            get_baseline = getattr(self.store, "engineering_baseline", None)
            if session_id is not None and get_baseline:
                session_baseline = get_baseline(task.job_id, task.step_id) or {}
                starting_commit = session_baseline.get("starting_commit") or starting_commit
            start_session = getattr(self.store, "start_engineering_session", None)
            if session_id is None and start_session:
                session_id = start_session(task.job_id, task.step_id, self.worker_id, starting_commit)
            current_branch = git.branch()
            if current_branch != task.branch:
                raise RuntimeError(
                    f"expected branch {task.branch!r}, found {current_branch!r}; branch changes require human setup"
                )
            if "preexisting_changes" in session_baseline:
                initial_changes = set(session_baseline.get("preexisting_changes") or [])
            else:
                initial_changes = set(git.changed_files())
            self.store.event(task, "resumed" if (session_baseline or session_state) else "started", {
                "starting_commit": starting_commit,
                "preexisting_changes": sorted(initial_changes),
                "session_id": session_id,
            })
            list_files = getattr(git, "files", None)
            inventory = getattr(workspace, "list_files", None)
            if list_files:
                repository_files = list_files()
            elif inventory:
                repository_files = inventory()
            else:
                repository_files = []
            backend = self.router.choose(task.attempt)
            if session_state.get("current_model_tier") in {"standard", "premium", "openrouter"}:
                # Resume the last selected tier when possible. The router may
                # still fall back to local if cloud credentials are absent.
                route_attempt = int(getattr(self.router, "escalate_after", task.attempt + 2))
                if session_state.get("current_model_tier") == "premium":
                    route_attempt += 1
                backend = self.router.choose(route_attempt, True)
            context = {
                "task": {
                    "job_id": task.job_id, "step_id": task.step_id,
                    "objective": task.objective,
                    "acceptance_criteria": task.acceptance_criteria,
                    "constraints": task.constraints,
                    "reviewer_feedback": task.reviewer_feedback,
                },
                "repository": str(workspace.root), "branch": current_branch,
                "repository_files": repository_files,
                "starting_commit": starting_commit,
                "preexisting_changes": sorted(initial_changes),
                "project_instructions": workspace.project_instructions(),
                "session_resume": {
                    "turn_count": int(session_state.get("turn_count") or 0),
                    "last_action": session_state.get("last_action"),
                    "last_observation": self._redact(
                        str(session_state.get("last_observation") or "")
                    )[:12_000],
                    "last_progress": session_state.get("last_progress"),
                    "stagnation_count": int(session_state.get("stagnation_count") or 0),
                    "model_tier": session_state.get("current_model_tier") or "local",
                    "last_test_result": session_state.get("last_test_result"),
                } if session_state else None,
            }
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": bounded_json(
                            context, self.max_prompt_chars, "engineering context"
                        )}]
            last_model = session_state.get("last_model") or backend.model
            last_test_result = session_state.get("last_test_result") or {}
            if isinstance(last_test_result, str):
                try:
                    last_test_result = json.loads(last_test_result)
                except json.JSONDecodeError:
                    last_test_result = {}
            verified = bool(last_test_result.get("passed")) if isinstance(last_test_result, dict) else False
            previous_observation = str(session_state.get("last_observation") or "")
            previous_fingerprint = self._fingerprint(previous_observation) if previous_observation else None
            previous_repo_signature = self._repository_signature(git)
            recent_fingerprints: list[str] = []
            if previous_fingerprint:
                recent_fingerprints.append(previous_fingerprint)
            repeated_failures: dict[str, int] = {}
            stagnation = max(0, int(session_state.get("stagnation_count") or 0))
            stagnation_episodes = 0
            escalation_level = 0
            turn = max(0, int(session_state.get("turn_count") or 0))
            while self.max_turns is None or turn < self.max_turns:
                turn += 1
                controlled = self._controlled_result(task, last_model)
                if controlled is not None:
                    return controlled
                heartbeat_ok = self.store.heartbeat(task.step_id, self.worker_id)
                if not heartbeat_ok:
                    controlled = self._controlled_result(task, last_model)
                    if controlled is not None:
                        return controlled
                    raise RuntimeError("engineering lease is no longer owned")
                if stagnation >= 3:
                    stagnation_episodes += 1
                    if stagnation_episodes > self.max_stagnation_episodes:
                        blocker = (
                            "engineering stagnation detected after "
                            f"{stagnation_episodes} recovery episodes; resume to retry"
                        )
                        result = AgentResult(
                            Status.BLOCKED,
                            "engineering paused after repeated non-progress",
                            blocker=blocker,
                            model=last_model,
                        )
                        self.store.update_step(
                            task.step_id, self.worker_id, result.status,
                            blocker=result.blocker, model_used=last_model,
                            coder_response={"reason": "stagnation_detected", "turns": turn},
                        )
                        return result
                    escalation_level += 1
                    first_cloud_attempt = int(getattr(
                        self.router, "escalate_after", task.attempt + 3
                    ))
                    route_attempt = max(
                        task.attempt + 2 + escalation_level,
                        first_cloud_attempt + escalation_level - 1,
                    )
                    backend = self.router.choose(route_attempt, True)
                    last_model = backend.model
                    stagnation = 0
                request_messages = bounded_messages(messages, self.max_prompt_chars)
                self.last_prompt_chars = message_chars(request_messages)
                response = backend.complete(request_messages)
                last_model = response.model
                try:
                    action = self._parse_action(response.text)
                    observation = self._execute(action, workspace, runner, git, task,
                                                initial_changes)
                except (ValueError, KeyError, OSError, WorkspaceViolation) as exc:
                    # Invalid model actions (for example trying to read the
                    # virtual ``repository`` label as a file) are recoverable:
                    # feed the precise error back to the model instead of
                    # crashing the durable coding lease.
                    action = {"action": "invalid"}
                    observation = f"Action rejected: {exc}. Choose a valid path relative to the repository root."
                    messages.extend([{"role": "assistant", "content": response.text},
                                     {"role": "user", "content": observation}])
                run_succeeded = False
                run_result: dict[str, Any] = {}
                repeated_failure = False
                if action.get("action") == "run":
                    try:
                        run_result = json.loads(observation)
                        run_succeeded = run_result.get("exit_code") == 0
                        if not run_succeeded:
                            run_key = json.dumps({
                                "argv": action.get("argv", []),
                                "exit_code": run_result.get("exit_code"),
                                "timed_out": run_result.get("timed_out", False),
                                "cancelled": run_result.get("cancelled", False),
                            }, sort_keys=True)
                            repeated_failures[run_key] = repeated_failures.get(run_key, 0) + 1
                            repeated_failure = repeated_failures[run_key] >= 2
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        run_succeeded = False
                fingerprint = self._fingerprint(observation)
                oscillation = (
                    len(recent_fingerprints) >= 2
                    and fingerprint == recent_fingerprints[-2]
                    and fingerprint != recent_fingerprints[-1]
                )
                repo_signature = self._repository_signature(git)
                repository_progress = (
                    repo_signature is not None
                    and previous_repo_signature is not None
                    and repo_signature != previous_repo_signature
                )
                same_observation = (
                    fingerprint is not None and fingerprint == previous_fingerprint
                )
                if action.get("action") == "invalid":
                    progress_classification = "invalid_action"
                elif run_succeeded:
                    progress_classification = "verification_progress"
                elif run_result.get("cancelled"):
                    progress_classification = "cancelled"
                elif repeated_failure:
                    progress_classification = "repeated_failure"
                elif oscillation:
                    progress_classification = "oscillation"
                elif action.get("action") == "finish" and not verified:
                    progress_classification = "no_verification_progress"
                elif repository_progress:
                    progress_classification = "repository_change"
                elif action.get("action") in {"write", "delete"}:
                    progress_classification = "repository_change"
                elif not same_observation and action.get("action") in {"read", "inspect"}:
                    progress_classification = "inspection_progress"
                else:
                    progress_classification = "no_progress"
                stagnation_next = (
                    stagnation + 1
                    if (
                        action.get("action") in {"invalid", "blocked"}
                        or progress_classification in {
                            "no_progress", "repeated_failure", "oscillation",
                            "no_verification_progress", "cancelled",
                        }
                    ) else 0
                )
                if action["action"] == "run":
                    try:
                        verified = verified or json.loads(observation)["exit_code"] == 0
                    except (json.JSONDecodeError, KeyError):
                        pass
                self.store.event(task, "agent_action", {
                    "turn": turn, "model": response.model,
                    "action": action.get("action"), "observation": self._redact(observation),
                    "progress_classification": progress_classification,
                    "failure_class": (
                        "environment" if run_result.get("exit_code") == 127 else "test"
                    ) if run_result and not run_succeeded else None,
                })
                if session_id is not None:
                    record_action = getattr(self.store, "record_engineering_action", None)
                    if record_action:
                        record_action(
                            session_id, turn, action.get("action", "invalid"),
                            self._redact(observation), response.model,
                            progress_classification,
                            provider=getattr(response, "backend", None),
                            model_tier=self._model_tier(response, backend),
                            stagnation_count=stagnation_next,
                            last_test_result=run_result if run_result else None,
                            current_problem=(self._redact(observation)
                                             if progress_classification in {
                                                 "repeated_failure", "no_verification_progress",
                                                 "oscillation", "invalid_action"
                                             } else None),
                        )
                if stagnation_next:
                    stagnation = stagnation_next
                else:
                    stagnation = 0
                    if escalation_level:
                        # A resolved difficult turn returns routine work to the
                        # local model, preserving cloud budget for the next
                        # genuinely stalled problem.
                        backend = self.router.choose(task.attempt, False)
                        last_model = backend.model
                        escalation_level = 0
                        stagnation_episodes = 0
                previous_observation = observation
                previous_fingerprint = fingerprint
                if repo_signature is not None:
                    previous_repo_signature = repo_signature
                recent_fingerprints.append(fingerprint)
                del recent_fingerprints[:-8]
                if action["action"] == "blocked":
                    result = AgentResult(Status.BLOCKED, "implementation blocked",
                                         blocker=str(action.get("reason", "unspecified")),
                                         model=last_model)
                    self.store.update_step(task.step_id, self.worker_id, result.status,
                                           blocker=result.blocker, model_used=last_model)
                    return result
                if action["action"] == "finish":
                    changed = git.changed_files()
                    new_changes = set(changed) - initial_changes
                    if not new_changes:
                        observation = "finish rejected: no task-owned file changes detected"
                    elif not verified:
                        observation = "finish rejected: no successful verification command recorded"
                    elif secret_files := self._files_with_likely_secrets(workspace, new_changes):
                        observation = f"finish rejected: likely secrets detected in {secret_files}"
                    else:
                        result = AgentResult(Status.REVIEW, str(action.get("summary", "")),
                                             sorted(new_changes), model=last_model,
                                             completed_at=datetime.now(UTC))
                        self.store.update_step(
                            task.step_id, self.worker_id, result.status,
                            starting_commit=starting_commit, files_changed=result.files_changed,
                            model_used=last_model,
                            coder_response={"summary": result.summary,
                                            "verification": action.get("verification", [])},
                        )
                        return result
                messages.extend([{"role": "assistant", "content": response.text},
                                 {"role": "user", "content": observation}])
            controlled = self._controlled_result(task, last_model)
            if controlled is not None:
                return controlled
            blocker = (
                f"agent turn budget exhausted after {self.max_turns} turns; "
                "this is an explicit compatibility limit; use progress-based mode "
                "or raise the compatibility limit"
            )
            result = AgentResult(
                Status.BLOCKED,
                "implementation paused at an explicit compatibility limit",
                blocker=blocker,
                model=last_model,
            )
            self.store.update_step(
                task.step_id,
                self.worker_id,
                result.status,
                blocker=result.blocker,
                model_used=last_model,
                coder_response={"reason": "compatibility_turn_limit", "turns": self.max_turns},
            )
            return result
        except Exception as exc:
            status = Status.FAILED
            failure_class = self._classify_failure(exc)
            blocker = str(exc)
            self.store.update_step(
                task.step_id,
                self.worker_id,
                status,
                blocker=blocker,
                coder_response={"failure_class": failure_class, "error": self._redact(blocker)},
            )
            self.store.event(task, "engineering_failed", {
                "failure_class": failure_class,
                "error": self._redact(blocker),
            })
            return AgentResult(
                status,
                "task did not reach review; return step for autonomous recovery",
                blocker=blocker,
                failure_class=failure_class,
            )

    @staticmethod
    def _classify_failure(exc: BaseException) -> str:
        """Map worker failures to stable operator/retry categories."""
        if isinstance(exc, BackendError):
            return "model"
        if isinstance(exc, (CommandRejected,)):
            return "tool_policy"
        if isinstance(exc, (WorkspaceViolation, GitError)):
            return "repository"
        if isinstance(exc, ValueError):
            return "model_action"
        if isinstance(exc, OSError):
            return "environment"
        module = type(exc).__module__
        if module == "psycopg" or module.startswith("psycopg."):
            return "database"
        return "internal"

    def _controlled_result(self, task: Task, model: str | None) -> AgentResult | None:
        """Stop at a safe turn boundary when an operator pauses or cancels."""
        get_status = getattr(self.store, "job_status", None)
        if get_status is None:
            return None
        status = get_status(task.job_id)
        if status not in {"paused", "cancelled"}:
            return None
        result_status = Status.PAUSED if status == "paused" else Status.CANCELLED
        summary = f"job {status}; EngineeringAgent stopped at a safe boundary"
        self.store.update_step(
            task.step_id,
            self.worker_id,
            Status.QUEUED,
            model_used=model,
            coder_response={"reason": f"job_{status}", "summary": summary},
        )
        self.store.event(task, f"engineering_{status}", {"reason": "operator_control", "model": model})
        return AgentResult(result_status, summary, model=model)

    def _execute(self, action: dict[str, Any], workspace: Workspace,
                 runner: CommandRunner, git: GitRepository, task: Task,
                 protected_changes: set[str]) -> str:
        kind = action["action"]
        canonical = getattr(workspace, "relative_path", None)
        normalize = canonical if callable(canonical) else str
        if kind == "read":
            return workspace.read_text(normalize(str(action["path"])))
        if kind == "write":
            path = normalize(str(action["path"]))
            if path in protected_changes:
                raise WorkspaceViolation(
                    f"refusing to overwrite pre-existing human change: {path}"
                )
            workspace.write_text(path, str(action["content"]))
            return f"wrote {path}"
        if kind == "delete":
            path = normalize(str(action["path"]))
            if path in protected_changes:
                raise WorkspaceViolation(
                    f"refusing to delete pre-existing human change: {path}"
                )
            workspace.delete(path, str(action.get("justification", "")))
            return f"deleted {path}"
        if kind == "run":
            argv = action.get("argv")
            if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
                raise ValueError("argv must be a string list")
            result = runner.run(
                argv,
                min(int(action.get("timeout", 300)), 900),
                cancel_check=lambda: self._job_controlled(task.job_id),
            )
            self.store.record_command(task.step_id, result, task.attempt)
            return json.dumps({"stdout": self._redact(result.stdout)[:12_000],
                               "stderr": self._redact(result.stderr)[:12_000],
                               "exit_code": result.exit_code,
                               "duration_seconds": result.duration_seconds,
                               "timed_out": result.timed_out,
                               "cancelled": result.cancelled})
        if kind == "inspect":
            inspect_kind = str(action["kind"])
            if inspect_kind == "status":
                return git.status()
            if inspect_kind == "diff":
                return git.diff()
            if inspect_kind == "instructions":
                return workspace.project_instructions()
            if inspect_kind == "tree":
                return "\n".join(git.files()) or "(repository has no tracked or standard untracked files)"
            raise ValueError(f"unknown inspection: {inspect_kind}")
        if kind in {"finish", "blocked"}:
            return kind
        raise ValueError(f"unknown action: {kind}")

    def _job_controlled(self, job_id: int) -> bool:
        """Return whether an in-flight command should stop at its boundary."""
        get_status = getattr(self.store, "job_status", None)
        if get_status is None:
            return False
        return get_status(job_id) in {"paused", "cancelled"}

    @staticmethod
    def _fingerprint(value: str) -> str:
        """Hash normalized observations so harmless whitespace cannot hide loops."""
        normalized = " ".join(str(value).split())
        return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _repository_signature(git: GitRepository) -> str | None:
        """Build a cheap semantic worktree signature for progress detection."""
        try:
            changed = sorted(git.changed_files())
            diff = git.diff()
        except Exception:
            # A failed diagnostic must not make an otherwise safe action crash;
            # the observation/action signals still provide stagnation evidence.
            return None
        material = json.dumps({"changed": changed, "diff": diff}, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _model_tier(response: Any, backend: Any) -> str:
        """Persist a coarse local/standard/premium tier for session resume."""
        provider = str(getattr(response, "backend", "") or "").lower()
        model = str(getattr(response, "model", "") or getattr(backend, "model", ""))
        if provider != "openrouter":
            return "local"
        if "premium" in model.lower() or "gpt-5" in model.lower() or "claude" in model.lower():
            return "premium"
        return "standard"

    @staticmethod
    def _files_with_likely_secrets(workspace: Workspace, files: set[str]) -> list[str]:
        secret_patterns = [
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE " + r"KEY-----"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
            re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
            re.compile(r"(?i)\b(?:api[_-]?key|password|secret)\s*=\s*['\"][^'\"]{8,}['\"]"),
        ]
        flagged: list[str] = []
        for relative in files:
            try:
                content = workspace.read_text(relative, max_bytes=1_000_000)
            except (OSError, UnicodeError, ValueError):
                continue
            if any(pattern.search(content) for pattern in secret_patterns):
                flagged.append(relative)
        return sorted(flagged)


# Backwards-compatible V1 import. New code should use EngineeringAgent.
CoderAgent = EngineeringAgent
