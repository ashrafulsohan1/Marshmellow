"""JSONL structured logging with secret scrubbing (01-architecture §3.15).

One JSON object per line: {ts, level, name, msg, exc?}. Secrets matching
`password=…`-style patterns are redacted from the fully formatted line —
the scrub runs after formatting so neither message arguments nor exception
text can leak. URL query credentials (`?key=…`, `&token=…` — monitor targets,
the M4 handoff) are redacted by a second, URL-shaped pass; the value class
stops at `&`/quote/whitespace so the non-credential parts of the URL and the
JSONL structure survive.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

_SECRET_RE = re.compile(
    r"(password|passwd|secret|api_key|apikey|token)\s*[=:]\s*(\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)

# Second pattern, applied after _SECRET_RE inside JsonFormatter.format: URL
# query params carrying credentials (monitor targets with query keys — the M4
# handoff; also exception text quoting such URLs). "?key=abc&x=1" →
# "?key=[REDACTED]&x=1" — the rest of the URL stays diagnosable. The value
# class also excludes the BACKSLASH: the scrub runs on the JSON-encoded line,
# so a value followed by a `\"` escape pair (a quoted URL in the logged text)
# must stop BEFORE the pair — consuming its backslash would leave an
# unescaped quote inside the JSON string and corrupt the row.
_URL_SECRET_RE = re.compile(
    r"[?&#](?:token|key|sig|signature|access_token|api_key|apikey)=([^&\"\\\s]+)", re.IGNORECASE,
)

_logger_name = "marshmellow"
_configured = False
_registered_secrets: list[str] = []


def register_secret(value: str) -> None:
    """Exact-string redaction for values that have no `password=` shape (API keys)."""
    if value and value not in _registered_secrets:
        _registered_secrets.append(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        line = json.dumps(entry, ensure_ascii=False)
        line = _SECRET_RE.sub(r"\1=[REDACTED]", line)
        line = _URL_SECRET_RE.sub(
            lambda m: m.group(0)[: -len(m.group(1))] + "[REDACTED]", line
        )
        for secret in _registered_secrets:
            if secret in line:
                line = line.replace(secret, "[REDACTED]")
        return line


def setup_logging(log_dir: Path) -> logging.Logger:
    """Configure the 'marshmellow' logger once; later calls are no-ops."""
    global _configured
    lg = logging.getLogger(_logger_name)
    if _configured:
        return lg
    log_dir.mkdir(parents=True, exist_ok=True)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    log_file = log_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    lg.addHandler(handler)
    _configured = True
    logging.getLogger().addHandler(logging.NullHandler())
    return lg


def _reset_for_tests() -> None:
    """Test-only: drop the singleton so setup_logging reconfigures cleanly."""
    global _configured
    _registered_secrets.clear()
    lg = logging.getLogger(_logger_name)
    for handler in list(lg.handlers):
        lg.removeHandler(handler)
        handler.close()
    _configured = False
