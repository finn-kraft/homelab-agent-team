from __future__ import annotations

import io
import json
import logging

from agent_core.structured_logging import StructuredJsonFormatter, log_event


def _logger(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger("tests.structured")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJsonFormatter("test-service"))
    logger.addHandler(handler)
    return logger


def test_structured_log_is_machine_readable_and_redacts_sensitive_fields():
    stream = io.StringIO()
    logger = _logger(stream)

    log_event(
        logger,
        logging.INFO,
        "package_claimed",
        package_id=17,
        token="top-secret-token",
        database_url="postgresql://user:password@db/agents",
        prompt="private repository instructions",
        command_output="private test output",
        detail={"diff": "nested private patch", "safe": "visible"},
    )

    record = json.loads(stream.getvalue())
    rendered = stream.getvalue()
    assert record["service"] == "test-service"
    assert record["event"] == "package_claimed"
    assert record["package_id"] == 17
    assert record["token"] == "[REDACTED]"
    assert record["database_url"] == "[REDACTED]"
    assert record["prompt"] == "[OMITTED]"
    assert record["command_output"] == "[OMITTED]"
    assert record["detail"] == {"diff": "[OMITTED]", "safe": "visible"}
    assert "top-secret-token" not in rendered
    assert "user:password" not in rendered
    assert "private repository instructions" not in rendered
    assert record["timestamp"].endswith("Z")


def test_structured_log_redacts_plain_messages_and_exception_details():
    stream = io.StringIO()
    logger = _logger(stream)

    try:
        raise RuntimeError("password=hunter2")
    except RuntimeError:
        logger.exception("request failed token=secret-value")

    record = json.loads(stream.getvalue())
    assert "hunter2" not in stream.getvalue()
    assert "secret-value" not in stream.getvalue()
    assert record["exception"]["type"] == "RuntimeError"
    assert record["exception"]["message"] == "[OMITTED]"
    assert "traceback" not in record["exception"]
