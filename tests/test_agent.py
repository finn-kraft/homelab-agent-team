import pytest

from coder_agent.agent import CoderAgent


def test_action_requires_json_object():
    with pytest.raises(ValueError):
        CoderAgent._parse_action("hello")
    with pytest.raises(ValueError):
        CoderAgent._parse_action("[]")
    assert CoderAgent._parse_action('{"action":"inspect","kind":"diff"}')["action"] == "inspect"


def test_redacts_credentials():
    text = CoderAgent._redact("api_key=super-secret-value ghp_abcdefghijklmnopqrstuvwxyz")
    assert "super-secret-value" not in text
    assert "ghp_" not in text

