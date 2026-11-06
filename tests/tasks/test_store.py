
import logging
from datetime import datetime
from pathlib import Path

from marshmellow.core.contracts import MonitorSpec, TaskSpec
from marshmellow.tasks.store import StoredTask, TaskStore, default_task_store_path


def _monitor_row(
    task_id: str = "m1", baseline: str | None = "v1", state: str = "waiting"
) -> StoredTask:
    return StoredTask(
        spec=TaskSpec(
            id=task_id,
            instruction="watch the page",
            mode="background_monitor",
            monitor=MonitorSpec(
                kind="url_diff",
                target="http://example.com",
                interval_s=60,
                condition="the page changes",
            ),
        ),
        baseline=baseline,
        state=state,  # type: ignore[arg-type]
    )


def test_store_round_trip(tmp_path: Path) -> None:
    """save → load returns the same rows (pydantic equality over the JSON round
    trip); a missing store file is a fresh install — no tasks, no crash."""
    store = TaskStore(tmp_path / "tasks.json")
    assert store.load() == [], "a missing store file yields no tasks"

    rows = [
        _monitor_row("m1", baseline="v1"),
        StoredTask(
            spec=TaskSpec(
                id="s1",
                instruction="open notepad",
                mode="scheduled",
                schedule=datetime(2026, 11, 7, 9, 0, 0),
            ),
            baseline=None,
            state="paused",
        ),
    ]
    store.save(rows)

    loaded = TaskStore(tmp_path / "tasks.json").load()
    assert loaded == rows
    assert loaded[0].baseline == "v1", "the anti-re-report record survives the round trip"


def test_malformed_store_yields_empty_with_warning(tmp_path: Path, caplog) -> None:
    """config.py convention: a malformed store never refuses to start the app —
    it is logged and discarded."""
    path = tmp_path / "tasks.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="marshmellow.tasks.store"):
        assert TaskStore(path).load() == []

    assert any("Malformed task store" in r.message for r in caplog.records)


def test_store_save_creates_missing_directories(tmp_path: Path) -> None:
    """save() creates parent dirs (config.save convention) — the default store
    path's Marshmellow/ dir may not exist on first ever run."""
    store = TaskStore(tmp_path / "Marshmellow" / "tasks.json")
    store.save([_monitor_row()])
    assert TaskStore(tmp_path / "Marshmellow" / "tasks.json").load() != []


def test_default_task_store_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_task_store_path() == tmp_path / "Marshmellow" / "tasks.json"
