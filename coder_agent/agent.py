from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from .commands import CommandRejected, CommandRunner
from .db import Store
from .git import GitRepository
from .llm import BackendError, Router
from .models import AgentResult, Status, Task, WorkPackage
from agent_core.prompt_budget import (
    DEFAULT_PROMPT_CHARS,
    bounded_json,
    bounded_messages,
    message_chars,
)
from .workspace import Workspace, WorkspaceViolation


SYSTEM_PROMPT = """You are an Engineering Worker, the implementation owner of one Work Package.
Inspect the repository and project instructions first, form and revise your implementation
plan internally, then implement, test, debug, and prepare a reviewable candidate. The
reviewer owns approval, but reviewer feedback returns to this same package session.
Work only within the package objective and constraints.
Return exactly one JSON object per turn, with one action:
{"action":"read","path":"relative/path"}
{"action":"write","path":"relative/path","content":"complete file content"}
{"action":"delete","path":"relative/path","justification":"specific reason"}
{"action":"run","argv":["pytest","-q"],"timeout":300}
{"action":"inspect","kind":"status|diff|instructions"}
{"action":"finish","summary":"...","verification":["..."]}
{"action":"blocked","reason":"..."}
Never claim a command passed unless its recorded exit code is zero. Do not commit, push,
change branches, access secrets, or expand the assignment. Prefer small, reviewable edits.
"""


class EngineeringAgent:
    def __init__(self, store: Store, router: Router, worker_id: str,
                 allowed_roots: list[str], max_turns: int = 30, max_attempts: int = 4,
                 max_prompt_chars: int = DEFAULT_PROMPT_CHARS):
        self.store, self.router, self.worker_id = store, router, worker_id
        self.allowed_roots, self.max_turns, self.max_attempts = allowed_roots, max_turns, max_attempts
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
            resume_session = getattr(self.store, "resume_engineering_session", None)
            if resume_session:
                session_id = resume_session(task.job_id, task.step_id)
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
            self.store.event(task, "resumed" if session_baseline else "started", {
                "starting_commit": starting_commit,
                "preexisting_changes": sorted(initial_changes),
                "session_id": session_id,
            })
            backend = self.router.choose(task.attempt)
            context = {
                "task": {
                    "job_id": task.job_id, "step_id": task.step_id,
                    "objective": task.objective,
                    "acceptance_criteria": task.acceptance_criteria,
                    "constraints": task.constraints,
                    "reviewer_feedback": task.reviewer_feedback,
                },
                "repository": str(workspace.root), "branch": current_branch,
                "starting_commit": starting_commit,
                "preexisting_changes": sorted(initial_changes),
                "project_instructions": workspace.project_instructions(),
            }
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": bounded_json(
                            context, self.max_prompt_chars, "engineering context"
                        )}]
            last_model = backend.model
            verified = False
            previous_observation = ""
            stagnation = 0
            escalation_level = 0
            for turn in range(self.max_turns):
                controlled = self._controlled_result(task, last_model)
                if controlled is not None:
                    return controlled
                heartbeat_ok = self.store.heartbeat(task.step_id, self.worker_id)
                if not heartbeat_ok:
                    controlled = self._controlled_result(task, last_model)
                    if controlled is not None:
                        return controlled
                    raise RuntimeError("coder lease is no longer owned")
                if stagnation >= 3:
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
                if action.get("action") == "run":
                    try:
                        run_succeeded = json.loads(observation).get("exit_code") == 0
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        run_succeeded = False
                if action.get("action") == "invalid":
                    progress_classification = "invalid_action"
                elif run_succeeded:
                    progress_classification = "verification_progress"
                elif action.get("action") in {"write", "delete"}:
                    progress_classification = "repository_change"
                elif observation != previous_observation and action.get("action") in {"read", "inspect"}:
                    progress_classification = "inspection_progress"
                else:
                    progress_classification = "no_progress"
                if action["action"] == "run":
                    try:
                        verified = verified or json.loads(observation)["exit_code"] == 0
                    except (json.JSONDecodeError, KeyError):
                        pass
                self.store.event(task, "agent_action", {
                    "turn": turn + 1, "model": response.model,
                    "action": action.get("action"), "observation": self._redact(observation),
                    "progress_classification": progress_classification,
                })
                if session_id is not None:
                    record_action = getattr(self.store, "record_engineering_action", None)
                    if record_action:
                        record_action(session_id, turn + 1, action.get("action", "invalid"),
                                      self._redact(observation), response.model,
                                      progress_classification)
                if observation == previous_observation or action.get("action") == "invalid":
                    stagnation += 1
                else:
                    stagnation = 0
                    if escalation_level:
                        # A resolved difficult turn returns routine work to the
                        # local model, preserving cloud budget for the next
                        # genuinely stalled problem.
                        backend = self.router.choose(task.attempt, False)
                        last_model = backend.model
                        escalation_level = 0
                previous_observation = observation
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
                "review progress before resuming"
            )
            result = AgentResult(
                Status.BLOCKED,
                "implementation paused at the configured turn budget",
                blocker=blocker,
                model=last_model,
            )
            self.store.update_step(
                task.step_id,
                self.worker_id,
                result.status,
                blocker=result.blocker,
                model_used=last_model,
                coder_response={"reason": "turn_budget_exhausted", "turns": self.max_turns},
            )
            return result
        except (BackendError, WorkspaceViolation, CommandRejected, RuntimeError, ValueError) as exc:
            status = Status.FAILED
            self.store.update_step(
                task.step_id,
                self.worker_id,
                status,
                blocker=str(exc),
            )
            return AgentResult(
                status,
                "task did not reach review; return step for autonomous recovery",
                blocker=str(exc),
            )

    def _controlled_result(self, task: Task, model: str | None) -> AgentResult | None:
        """Stop at a safe turn boundary when an operator pauses or cancels."""
        get_status = getattr(self.store, "job_status", None)
        if get_status is None:
            return None
        status = get_status(task.job_id)
        if status not in {"paused", "cancelled"}:
            return None
        result_status = Status.PAUSED if status == "paused" else Status.CANCELLED
        summary = f"job {status}; coder stopped at a safe boundary"
        self.store.update_step(
            task.step_id,
            self.worker_id,
            Status.QUEUED,
            model_used=model,
            coder_response={"reason": f"job_{status}", "summary": summary},
        )
        self.store.event(task, f"coding_{status}", {"reason": "operator_control", "model": model})
        return AgentResult(result_status, summary, model=model)

    def _execute(self, action: dict[str, Any], workspace: Workspace,
                 runner: CommandRunner, git: GitRepository, task: Task,
                 protected_changes: set[str]) -> str:
        kind = action["action"]
        if kind == "read":
            return workspace.read_text(str(action["path"]))
        if kind == "write":
            path = str(action["path"])
            if path in protected_changes:
                raise WorkspaceViolation(
                    f"refusing to overwrite pre-existing human change: {path}"
                )
            workspace.write_text(path, str(action["content"]))
            return f"wrote {action['path']}"
        if kind == "delete":
            path = str(action["path"])
            if path in protected_changes:
                raise WorkspaceViolation(
                    f"refusing to delete pre-existing human change: {path}"
                )
            workspace.delete(path, str(action.get("justification", "")))
            return f"deleted {action['path']}"
        if kind == "run":
            argv = action.get("argv")
            if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
                raise ValueError("argv must be a string list")
            result = runner.run(argv, min(int(action.get("timeout", 300)), 900))
            self.store.record_command(task.step_id, result, task.attempt)
            return json.dumps({"stdout": self._redact(result.stdout)[:12_000],
                               "stderr": self._redact(result.stderr)[:12_000],
                               "exit_code": result.exit_code,
                               "duration_seconds": result.duration_seconds,
                               "timed_out": result.timed_out})
        if kind == "inspect":
            inspect_kind = str(action["kind"])
            if inspect_kind == "status":
                return git.status()
            if inspect_kind == "diff":
                return git.diff()
            if inspect_kind == "instructions":
                return workspace.project_instructions()
            raise ValueError(f"unknown inspection: {inspect_kind}")
        if kind in {"finish", "blocked"}:
            return kind
        raise ValueError(f"unknown action: {kind}")

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
