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


def raw_log_text(log_dir: Path) -> str:
    """The whole session file as written — msg AND exc text, unfiltered."""
    files = sorted(log_dir.glob("session-*.jsonl"))
    assert files, "no session log written"
    return files[0].read_text(encoding="utf-8")


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


# -- URL query-param scrubbing (§3.15, M5 Task 6 — the M4 monitor-target handoff) --

URL_PARAM_CASES = [
    ("token", "tok1"),
    ("key", "k1"),
    ("sig", "sg1"),
    ("signature", "sg2"),
    ("access_token", "at1"),
    ("api_key", "ak1"),
    ("apikey", "ak2"),
]


@pytest.mark.parametrize(("param", "value"), URL_PARAM_CASES)
def test_url_query_credentials_redacted_in_msg(
    param: str, value: str, tmp_path: Path
) -> None:
    """A monitor target like `https://x/f?<param>=<value>&n=3` logged raw must
    lose the credential — whatever `password=`-shape it does or does not have."""
    logger = setup_logging(tmp_path)
    logger.info("GET %s please", f"https://example.com/f?{param}={value}&n=3")
    msg = str(read_lines(tmp_path)[-1]["msg"])
    assert value not in msg, f"{param}={value} leaked into the session log"
    assert f"{param}=[REDACTED]" in msg


def test_url_query_key_redacted_with_tail_preserved(tmp_path: Path) -> None:
    """`?key=abc&x=1` → `?key=[REDACTED]&x=1` — the non-credential parts of the
    URL survive (a monitor target must stay diagnosable after scrubbing)."""
    logger = setup_logging(tmp_path)
    logger.info("watch https://api.example.com/feed?key=abc123&x=1 for changes")
    msg = str(read_lines(tmp_path)[-1]["msg"])
    assert "abc123" not in msg
    assert "?key=[REDACTED]&x=1" in msg, f"URL tail was destroyed: {msg}"


def test_url_query_credentials_redacted_in_exc_text(tmp_path: Path) -> None:
    """The scrub runs on the fully formatted JSON line — exception text is
    inside that line, so URL credentials there are redacted too."""
    logger = setup_logging(tmp_path)
    try:
        raise RuntimeError(
            "GET https://a.example/f?token=tok9&x=1 failed; "
            "retry https://b.example/f?key=kk9&y=2"
        )
    except RuntimeError:
        logger.exception("provider fetch failed")
    raw = raw_log_text(tmp_path)
    assert "tok9" not in raw
    assert "kk9" not in raw
    assert "?key=[REDACTED]&y=2" in raw, "exc text was not scrubbed with the URL pattern"
    entries = read_lines(tmp_path)
    assert any("exc" in entry for entry in entries), "no exc field written"


def test_token_split_across_formatted_args_is_caught(tmp_path: Path) -> None:
    """A credential that only exists AFTER %-arg substitution is still caught:
    the scrub runs post-format, never on the raw format string."""
    logger = setup_logging(tmp_path)
    logger.info("GET %s timed out", "https://example.com/f?token=spl1t&x=1")
    msg = str(read_lines(tmp_path)[-1]["msg"])
    assert "spl1t" not in msg
