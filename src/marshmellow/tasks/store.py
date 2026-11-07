"""TaskStore — JSON persistence for background tasks across restart (§3.15, M4 T5).

The point of the store is the *baseline*: a monitor's last seen check value
travels with the task, so the first check after a restart COMPARES against it
instead of re-capturing — a restart never re-reports an already-seen change
(M4 review focus #2: "a lost baseline would re-announce the same upload on
every app start — spam = trust death").

The config.py convention applies to the store file: a missing file is a fresh
install (no tasks, no warning); a malformed one is logged and discarded — the
app never refuses to start over its task store.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from marshmellow.core.contracts import TaskSpec, TaskState

logger = logging.getLogger(__name__)


class StoredTask(BaseModel):
    """One persisted task row: the spec, the anti-re-report record, the state."""

    spec: TaskSpec
    baseline: str | None = None  # last seen check value — the anti-re-report record
    state: TaskState = "waiting"  # live states only; "paused" restores paused


_LIST = TypeAdapter(list[StoredTask])


class TaskStore:
    """Read/write the JSON task file; one save is the whole store (small file)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[StoredTask]:
        """Stored rows, or [] when the file is missing/malformed (config.py convention)."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _LIST.validate_python(data)
        except Exception:
            logger.warning("Malformed task store %s; starting with no tasks", self._path)
            return []

    def save(self, tasks: list[StoredTask]) -> None:
        """Atomic write: serialize first, then temp-file + ``os.replace``.

        A concurrent reader (or AV/indexer) holding the destination open for a
        moment must not turn a persist into a lost snapshot on Windows —
        unique temp name plus a brief replace-retry; on final refusal the tmp
        is cleaned up and the caller's best-effort logging takes over (the
        next persist retries anyway)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([row.model_dump(mode="json") for row in tasks], indent=2)
        tmp = self._path.with_name(self._path.name + "." + uuid.uuid4().hex + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == 4:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.02 * (attempt + 1))


def default_task_store_path() -> Path:
    """``%LOCALAPPDATA%/Marshmellow/tasks.json`` (the product location, like
    config.toml §3.14); a cwd fallback when LOCALAPPDATA is unset (non-Windows
    dev), mirroring the log-dir default in app.py."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.cwd()) / "Marshmellow" / "tasks.json"
