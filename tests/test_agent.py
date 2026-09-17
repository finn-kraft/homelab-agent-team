from contextlib import contextmanager

import pytest

from coder_agent.agent import CoderAgent
from coder_agent.db import Store


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
