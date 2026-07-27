"""#73: level-configurable, injection-safe structured logging."""

from __future__ import annotations

import json
import logging

import pytest

from common.logging_setup import _MARKER, setup_logging


@pytest.fixture(autouse=True)
def _clean_root():
    """Remove this module's handlers and restore the level after each test."""
    root = logging.getLogger()
    before_level = root.level
    yield
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
    root.setLevel(before_level)


def _our_handler() -> logging.Handler:
    return next(h for h in logging.getLogger().handlers if getattr(h, _MARKER, False))


def _render(record: logging.LogRecord) -> str:
    return _our_handler().formatter.format(record)


def _record(msg: str, level: int = logging.INFO, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("common.claims", level, __file__, 1, msg, None, exc_info)


class TestLevels:
    def test_level_comes_from_env(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        setup_logging("ingestion-api")
        assert logging.getLogger().level == logging.DEBUG

    def test_default_is_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        setup_logging("ingestion-api")
        assert logging.getLogger().level == logging.INFO

    def test_invalid_level_falls_back_instead_of_crashing(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        setup_logging("ingestion-api")
        assert logging.getLogger().level == logging.INFO

    def test_repeated_setup_does_not_stack_handlers(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        setup_logging("ingestion-api")
        setup_logging("ingestion-api")
        ours = [h for h in logging.getLogger().handlers if getattr(h, _MARKER, False)]
        assert len(ours) == 1


class TestTextFormat:
    def test_line_carries_ts_level_service_logger(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "text")
        setup_logging("orchestration-mcp")
        line = _render(_record("hello"))
        assert " INFO [orchestration-mcp] common.claims: hello" in line

    def test_control_characters_cannot_forge_a_second_line(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "text")
        setup_logging("orchestration-mcp")
        line = _render(_record("user\n2026-01-01 ERROR forged: no"))
        assert "\n" not in line
        assert "\\x0a" in line


class TestJsonFormat:
    def test_one_valid_json_object_per_line(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        setup_logging("ingestion-worker")
        line = _render(_record("processed", level=logging.WARNING))
        parsed = json.loads(line)
        assert parsed["level"] == "WARNING"
        assert parsed["service"] == "ingestion-worker"
        assert parsed["logger"] == "common.claims"
        assert parsed["msg"] == "processed"

    def test_hostile_message_stays_inside_the_json_string(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        setup_logging("ingestion-worker")
        line = _render(_record('x\n{"level": "ERROR", "msg": "forged"}'))
        assert "\n" not in line
        assert json.loads(line)["msg"].startswith("x\n")

    def test_exc_info_is_carried(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        setup_logging("ingestion-worker")
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            line = _render(_record("failed", level=logging.ERROR, exc_info=sys.exc_info()))
        assert "boom" in json.loads(line)["exc_info"]
