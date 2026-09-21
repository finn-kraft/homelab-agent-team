from agent_core.redaction import redact_payload, redact_text


def test_redaction_recurses_without_mutating_untrusted_payload():
    source = {
        "summary": "token=super-secret-value",
        "nested": ["safe", {"database": "postgresql://user:pass@db/work"}],
    }

    redacted = redact_payload(source)

    assert "super-secret-value" not in str(redacted)
    assert "user:pass" not in str(redacted)
    assert redacted["nested"][0] == "safe"
    assert source["summary"] == "token=super-secret-value"
    assert redact_text("password=hunter2") == "[REDACTED]"
