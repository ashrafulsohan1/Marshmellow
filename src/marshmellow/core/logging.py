"""JSONL structured logging with secret scrubbing (01-architecture §3.15).

One JSON object per line: {ts, level, name, msg, exc?}. Secrets matching
`password=…`-style patterns are redacted from the fully formatted line —
the scrub runs after formatting so neither message arguments nor exception
text can leak.
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
