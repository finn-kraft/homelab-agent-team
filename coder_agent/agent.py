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


class CoderAgent:
    def __init__(self, store: Store, router: Router, worker_id: str,
                 allowed_roots: list[str], max_turns: int = 30, max_attempts: int = 4):
        self.store, self.router, self.worker_id = store, router, worker_id
        self.allowed_roots, self.max_turns, self.max_attempts = allowed_roots, max_turns, max_attempts

    def run_package(self, package: WorkPackage | Task, step_id: int | None = None,
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
        if task.attempt > self.max_attempts:
            result = AgentResult(
                Status.FAILED,
                "retry limit reached; return step for replanning",
                blocker="maximum coder attempts exceeded",
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
            resume_session = getattr(self.store, "resume_engineering_session", None)
            if resume_session:
                session_id = resume_session(task.job_id, task.step_id)
            start_session = getattr(self.store, "start_engineering_session", None)
            if session_id is None and start_session:
                session_id = start_session(task.job_id, task.step_id, self.worker_id, starting_commit)
            current_branch = git.branch()
            if current_branch != task.branch:
                raise RuntimeError(
                    f"expected branch {task.branch!r}, found {current_branch!r}; branch changes require human setup"
                )
            initial_changes = set(git.changed_files())
            self.store.event(task, "started", {"starting_commit": starting_commit,
                                                "preexisting_changes": sorted(initial_changes)})
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
                        {"role": "user", "content": json.dumps(context)}]
            last_model = backend.model
            verified = False
            previous_observation = ""
            stagnation = 0
            for turn in range(self.max_turns):
                self.store.heartbeat(task.step_id, self.worker_id)
                if stagnation >= 3:
                    backend = self.router.choose(task.attempt + stagnation, True)
                    last_model = backend.model
                    stagnation = 0
                response = backend.complete(messages)
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
                if action["action"] == "run":
                    try:
                        verified = verified or json.loads(observation)["exit_code"] == 0
                    except (json.JSONDecodeError, KeyError):
                        pass
                self.store.event(task, "agent_action", {
                    "turn": turn + 1, "model": response.model,
                    "action": action.get("action"), "observation": self._redact(observation),
                    "progress_classification": (
                        "invalid_action" if action.get("action") == "invalid" else
                        "verification_progress" if action.get("action") == "run" and verified else
                        "repository_change" if action.get("action") in {"write", "delete"} else
                        "no_progress"
                    ),
                })
                if session_id is not None:
                    record_action = getattr(self.store, "record_engineering_action", None)
                    if record_action:
                        record_action(session_id, turn + 1, action.get("action", "invalid"),
                                      self._redact(observation), response.model,
                                      "invalid_action" if action.get("action") == "invalid" else None)
                if observation == previous_observation or action.get("action") == "invalid":
                    stagnation += 1
                else:
                    stagnation = 0
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
                        if session_id is not None:
                            complete_session = getattr(self.store, "complete_engineering_session", None)
                            if complete_session:
                                complete_session(session_id)
                        return result
                messages.extend([{"role": "assistant", "content": response.text},
                                 {"role": "user", "content": observation}])
            raise RuntimeError("maximum agent turns exceeded")
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
            return json.dumps({"stdout": self._redact(result.stdout),
                               "stderr": self._redact(result.stderr),
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
