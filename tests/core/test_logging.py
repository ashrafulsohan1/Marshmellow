import json
from pathlib import Path

import pytest

from marshmellow.core import logging as mlog
from marshmellow.core.logging import setup_logging


@pytest.fixture(autouse=True)
def _isolated_logging():
    mlog._reset_for_tests()
    yield
    mlog._reset_for_tests()


def read_lines(log_dir: Path) -> list[dict[str, object]]:
    files = sorted(log_dir.glob("session-*.jsonl"))
    assert files, "no session log written"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_jsonl_lines_parse(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    logger.info("hello %s", "world")
    entries = read_lines(tmp_path)
    assert entries[-1]["msg"] == "hello world"
    assert entries[-1]["level"] == "INFO"
    assert entries[-1]["name"] == "marshmellow"


def test_scrubber_redacts_secrets(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    logger.info("login attempt password=hunter2 with api_key=abc123 done")
    content = "\n".join(str(e["msg"]) for e in read_lines(tmp_path))
    assert "hunter2" not in content
    assert "abc123" not in content
    assert "password=[REDACTED]" in content


def test_setup_is_idempotent(tmp_path: Path) -> None:
    l1 = setup_logging(tmp_path)
    n = len(l1.handlers)
    l2 = setup_logging(tmp_path)
    assert len(l2.handlers) == n
    assert l1 is l2
