"""Tests for packages.shared.logging: JSON-structured log output."""

import json
import logging

from packages.shared.logging import configure_logging


def test_configure_logging_emits_valid_json(capsys):
    configure_logging(level="INFO")
    logger = logging.getLogger("test.logging")
    logger.info("hello world", extra={"foo": "bar"})

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logging"
    assert "timestamp" in payload
    assert payload["foo"] == "bar"
