import pytest

from coder_agent.agent import CoderAgent


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
