from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_core.llm import LLMResponse
from agent_core.prompt_budget import bounded_json, bounded_messages, bounded_text, message_chars
from coder_agent.agent import CoderAgent
from coder_agent.db import Store
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
    assert store.events[-1][0] == "coding_paused"


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
