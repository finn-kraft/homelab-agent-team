from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_core.llm import LLMResponse
from agent_core.prompt_budget import (
    bounded_json, bounded_messages, bounded_text, context_snapshot,
    estimate_tokens, message_chars,
)
from coder_agent.agent import CoderAgent
from coder_agent.cli import _engineering_turn_limit
from coder_agent.db import Store
from coder_agent.commands import CommandRejected
from coder_agent.git import GitError
from coder_agent.llm import BackendError
from coder_agent.models import Status, Task


def test_action_requires_json_object():
    with pytest.raises(ValueError):
        CoderAgent._parse_action("hello")
    with pytest.raises(ValueError):
        CoderAgent._parse_action("[]")
    assert CoderAgent._parse_action('{"action":"inspect","kind":"diff"}')["action"] == "inspect"


def test_redacts_credentials():
    assignment = "api_" + "key=" + "super-secret-value"
    token = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
    text = CoderAgent._redact(f"{assignment} {token}")
    assert "super-secret-value" not in text
    assert "gh" + "p_" not in text


def test_failure_categories_are_stable_for_operator_recovery():
    assert CoderAgent._classify_failure(BackendError("backend circuit open")) == "model"
    assert CoderAgent._classify_failure(CommandRejected("unsafe")) == "tool_policy"
    assert CoderAgent._classify_failure(GitError("missing branch")) == "repository"


def test_prompt_history_keeps_system_and_newest_turns_within_budget():
    messages = [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "old" * 10_000},
        {"role": "assistant", "content": "new" * 10_000},
    ]

    bounded = bounded_messages(messages, 1_024)

    assert message_chars(bounded) <= 1_024
    assert bounded[0]["role"] == "system"
    assert "new" in bounded[-1]["content"]


def test_prompt_budget_truncation_includes_marker_without_exceeding_limit():
    assert len(bounded_text("x" * 100, 8)) <= 8
    assert len(bounded_text("x" * 100, 1)) <= 1
    assert len(bounded_json({"payload": "x" * 100}, 32)) <= 32


def test_prompt_budget_can_enforce_token_ceiling_and_hash_snapshot():
    messages = [{"role": "system", "content": "rules"},
                {"role": "user", "content": "x" * 10_000}]
    bounded = bounded_messages(messages, 10_000, max_tokens=100)
    assert estimate_tokens(bounded) <= 100
    snapshot = context_snapshot({"messages": messages}, max_chars=10_000, max_tokens=100)
    assert snapshot["truncated"] is True
    assert len(snapshot["sha256"]) == 64
    assert snapshot["tokens"] <= 100


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, rows):
        self.rows = iter(rows)

    def execute(self, *_args):
        return _FakeCursor(next(self.rows))


def test_engineering_session_helpers_support_default_psycopg_tuple_rows(monkeypatch):
    """The coder store uses tuple rows, including for V2 session helpers."""
    store = Store("postgresql://unused")
    connection = _FakeConnection([(17,), (23,)])

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(store, "connect", connect)
    assert store.start_engineering_session(1, 2, "coder-1") == 17
    assert store.resume_engineering_session(1, 2) == 23


def test_engineering_baseline_recovers_original_human_changes(monkeypatch):
    store = Store("postgresql://unused")
    connection = _FakeConnection([
        ("a" * 40,),
        ({"preexisting_changes": ["notes.txt"]},),
    ])

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(store, "connect", connect)
    baseline = store.engineering_baseline(1, 2)
    assert baseline == {
        "starting_commit": "a" * 40,
        "preexisting_changes": ["notes.txt"],
    }


def test_engineering_session_state_restores_last_action(monkeypatch):
    store = Store("postgresql://unused")
    connection = _FakeConnection([(
        17, 4, 2, "standard", "pytest failed", {"passed": False},
        "qwen", "openrouter", "run", '{"exit_code": 1}', "repeated_failure",
    )])

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(store, "connect", connect)
    state = store.engineering_session_state(1, 2)
    assert state["id"] == 17
    assert state["turn_count"] == 4
    assert state["last_action"] == "run"
    assert state["last_progress"] == "repeated_failure"


class _AgentStore:
    def __init__(self, status="running"):
        self.status = status
        self.updates = []
        self.events = []

    def job_status(self, _job_id):
        return self.status

    def heartbeat(self, *_args):
        return True

    def event(self, _task, kind, payload):
        self.events.append((kind, payload))

    def update_step(self, step_id, worker_id, status, **fields):
        self.updates.append((step_id, worker_id, status, fields))


class _ResumableStore(_AgentStore):
    def __init__(self):
        super().__init__("running")
        self.actions = []

    def resume_engineering_session(self, _job_id, _step_id):
        return 17

    def engineering_session_state(self, _job_id, _step_id):
        return {
            "id": 17, "turn_count": 1, "stagnation_count": 0,
            "current_model_tier": "local", "last_model": "local-test",
            "last_action": "inspect", "last_observation": "clean",
            "last_progress": "inspection_progress", "last_test_result": {},
        }

    def engineering_baseline(self, _job_id, _step_id):
        return {"starting_commit": "a" * 40, "preexisting_changes": []}

    def record_engineering_action(self, *args, **kwargs):
        self.actions.append((args, kwargs))


class _AgentWorkspace:
    def __init__(self, root, _allowed_roots):
        self.root = Path(root)

    def project_instructions(self):
        return ""


class _AgentRunner:
    def __init__(self, _workspace):
        pass


class _AgentGit:
    def __init__(self, _runner):
        pass

    def head(self):
        return "a" * 40

    def branch(self):
        return "agents/work"

    def changed_files(self):
        return []

    def status(self):
        return ""


class _AgentBackend:
    model = "local-test"

    def __init__(self, text):
        self.text = text

    def complete(self, _messages):
        return LLMResponse(self.text, self.model, "ollama")


class _FailingBackend(_AgentBackend):
    def complete(self, _messages):
        raise BackendError("backend circuit open; retry window has not elapsed")


class _AgentRouter:
    def __init__(self, backend):
        self.backend = backend

    def choose(self, *_args, **_kwargs):
        return self.backend


def _task(tmp_path):
    return Task(1, 2, str(tmp_path), "agents/work", "work", ["done"],
                status=Status.RUNNING, attempt=1)


def test_running_coder_stops_and_requeues_when_job_is_paused(monkeypatch, tmp_path):
    store = _AgentStore("paused")
    monkeypatch.setattr("coder_agent.agent.Workspace", _AgentWorkspace)
    monkeypatch.setattr("coder_agent.agent.CommandRunner", _AgentRunner)
    monkeypatch.setattr("coder_agent.agent.GitRepository", _AgentGit)
    agent = CoderAgent(store, _AgentRouter(_AgentBackend('{"action":"inspect","kind":"status"}')),
                       "coder-1", [str(tmp_path)], max_turns=10)

    result = agent.run_task(_task(tmp_path))

    assert result.status is Status.PAUSED
    assert store.updates[-1][2] is Status.QUEUED
    assert store.events[-1][0] == "engineering_paused"


def test_turn_budget_becomes_a_recoverable_blocker(monkeypatch, tmp_path):
    store = _AgentStore("running")
    monkeypatch.setattr("coder_agent.agent.Workspace", _AgentWorkspace)
    monkeypatch.setattr("coder_agent.agent.CommandRunner", _AgentRunner)
    monkeypatch.setattr("coder_agent.agent.GitRepository", _AgentGit)
    agent = CoderAgent(store, _AgentRouter(_AgentBackend('{"action":"inspect","kind":"status"}')),
                       "coder-1", [str(tmp_path)], max_turns=2)

    result = agent.run_task(_task(tmp_path))

    assert result.status is Status.BLOCKED
    assert "turn budget exhausted" in (result.blocker or "")
    assert store.updates[-1][2] is Status.BLOCKED


def test_model_failure_is_persisted_with_a_recovery_category(monkeypatch, tmp_path):
    store = _AgentStore("running")
    monkeypatch.setattr("coder_agent.agent.Workspace", _AgentWorkspace)
    monkeypatch.setattr("coder_agent.agent.CommandRunner", _AgentRunner)
    monkeypatch.setattr("coder_agent.agent.GitRepository", _AgentGit)
    agent = CoderAgent(store, _AgentRouter(_FailingBackend("unused")),
                       "engineering-1", [str(tmp_path)], max_turns=2)

    result = agent.run_task(_task(tmp_path))

    assert result.status is Status.FAILED
    assert result.failure_class == "model"
    assert store.events[-1] == ("engineering_failed", {
        "failure_class": "model",
        "error": "backend circuit open; retry window has not elapsed",
    })


def test_resumed_session_continues_sequence_and_includes_last_observation(monkeypatch, tmp_path):
    store = _ResumableStore()
    backend = _AgentBackend('{"action":"inspect","kind":"status"}')
    monkeypatch.setattr("coder_agent.agent.Workspace", _AgentWorkspace)
    monkeypatch.setattr("coder_agent.agent.CommandRunner", _AgentRunner)
    monkeypatch.setattr("coder_agent.agent.GitRepository", _AgentGit)
    agent = CoderAgent(store, _AgentRouter(backend), "engineering-1", [str(tmp_path)], max_turns=2)

    result = agent.run_task(_task(tmp_path))

    assert result.status is Status.BLOCKED
    assert store.actions and store.actions[0][0][1] == 2
    assert store.actions[0][1]["model_tier"] == "local"


def test_progress_mode_has_no_overall_turn_cutoff_and_stops_only_on_stagnation(monkeypatch, tmp_path):
    store = _AgentStore("running")
    monkeypatch.setattr("coder_agent.agent.Workspace", _AgentWorkspace)
    monkeypatch.setattr("coder_agent.agent.CommandRunner", _AgentRunner)
    monkeypatch.setattr("coder_agent.agent.GitRepository", _AgentGit)
    agent = CoderAgent(
        store,
        _AgentRouter(_AgentBackend('{"action":"inspect","kind":"status"}')),
        "engineering-1",
        [str(tmp_path)],
        max_turns=0,
        max_stagnation_episodes=1,
    )

    result = agent.run_task(_task(tmp_path))

    assert agent.max_turns is None
    assert result.status is Status.BLOCKED
    assert "stagnation" in (result.blocker or "")
    assert "turn budget exhausted" not in (result.blocker or "")


def test_legacy_turn_environment_cannot_reintroduce_the_30_turn_cutoff(monkeypatch):
    monkeypatch.delenv("ENGINEERING_TURN_LIMIT", raising=False)
    monkeypatch.setenv("ENGINEERING_MAX_TURNS", "30")
    monkeypatch.setenv("CODER_MAX_TURNS", "30")
    assert _engineering_turn_limit() is None
    monkeypatch.setenv("ENGINEERING_TURN_LIMIT", "45")
    assert _engineering_turn_limit() == 45
