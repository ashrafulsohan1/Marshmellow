"""Tasks dashboard tests — offscreen (QT_QPA_PLATFORM set in tests/conftest.py).

M4 Task 6 (§3.11): the main window mirrors ``TaskDirectory.list_tasks()`` with
per-row Pause/Resume/Run now/Delete controls and a live trace pane receiving
ALL TaskEvents (foreground steps + monitor waiting/result history, §3.12)
through a queued Qt Signal — engine threads never touch widgets. The tray's
``open_ui`` marshals onto the GUI thread before the dashboard is lazily built.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from marshmellow.app import App  # noqa: E402
from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.core.config import Settings  # noqa: E402
from marshmellow.core.contracts import (  # noqa: E402
    ActionRequest,
    ActionResult,
    MonitorSpec,
    PerceptionSnapshot,
    TaskEvent,
    TaskMode,
    TaskResult,
    TaskSpec,
    TaskState,
)
from marshmellow.planner.fake import FakePlanner  # noqa: E402
from marshmellow.stt.fake import FakeSTT  # noqa: E402
from marshmellow.tasks.manager import TaskHandle  # noqa: E402
from marshmellow.ui.main_window import TasksDashboard  # noqa: E402
from marshmellow.ui.tray import TrayAction  # noqa: E402

# -- doubles ---------------------------------------------------------------------


class FakeDirectory:
    """TaskDirectory double: canned handles + recorded control() calls."""

    def __init__(self, tasks: list[TaskHandle] | None = None) -> None:
        self.tasks = list(tasks or [])
        self.calls: list[tuple[str, str]] = []

    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]:
        if state is None:
            return list(self.tasks)
        return [t for t in self.tasks if t.state == state]

    def control(
        self, task_id: str, cmd: Literal["pause", "resume", "cancel", "run_now"]
    ) -> bool:
        self.calls.append((task_id, cmd))
        return True


def _handle(
    task_id: str,
    *,
    instruction: str = "watch the shared folder",
    mode: TaskMode = "background_monitor",
    state: TaskState = "waiting",
    result: TaskResult | None = None,
    error: str | None = None,
    next_due: datetime | None = None,
) -> TaskHandle:
    monitor = (
        MonitorSpec(kind="file_hash", target="C:/shared", interval_s=60, condition="changed")
        if mode == "background_monitor"
        else None
    )
    schedule = datetime(2026, 11, 6, 9, 0) if mode == "scheduled" else None
    return TaskHandle(
        spec=TaskSpec(
            id=task_id, instruction=instruction, mode=mode, monitor=monitor, schedule=schedule
        ),
        state=state,
        result=result,
        error=error,
        next_due=next_due,
    )


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _button(dash: TasksDashboard, cmd: str, task_id: str) -> QPushButton:
    button = dash.findChild(QPushButton, f"{cmd}:{task_id}")
    assert button is not None, f"no {cmd} button for {task_id}"
    return button


def _click(button: QPushButton) -> None:
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def _pump(qapp: QApplication, predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Pump the Qt loop until predicate() holds — queued signals land here."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition was not reached in time")
        qapp.processEvents()
        time.sleep(0.01)


# -- the task table ---------------------------------------------------------------


def test_table_mirrors_list_tasks(qapp: QApplication) -> None:
    tasks = FakeDirectory(
        [
            _handle("m1", instruction="watch the shared folder", state="waiting"),
            _handle(
                "s1",
                instruction="open my mail at 9",
                mode="scheduled",
                state="pending",
                next_due=datetime(2026, 11, 6, 9, 0),
            ),
        ]
    )
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    assert dash.table.rowCount() == len(tasks.list_tasks()) == 2
    assert dash.table.item(0, 1).text() == "watch the shared folder"
    assert dash.table.item(0, 2).text() == "waiting"
    assert dash.table.item(1, 1).text() == "open my mail at 9"
    assert dash.table.item(1, 2).text() == "pending"
    assert "2026-11-06 09:00" in dash.table.item(1, 4).text()  # next check column


def test_last_result_and_error_columns(qapp: QApplication) -> None:
    tasks = FakeDirectory(
        [
            _handle(
                "ok1",
                result=TaskResult(ok=True, summary_to_speak="the folder changed"),
                state="completed",
            ),
            _handle("err1", error="connection refused", state="waiting"),
        ]
    )
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    assert dash.table.item(0, 3).text() == "the folder changed"
    assert "connection refused" in dash.table.item(1, 3).text()


def test_dashboard_pause_button_calls_control(qapp: QApplication) -> None:
    tasks = FakeDirectory([_handle("m1", state="waiting")])
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    _click(_button(dash, "pause", "m1"))
    # The handler ran AND returned inline: the click already recorded the call —
    # a blocked GUI thread could never have reached this line.
    assert tasks.calls == [("m1", "pause")]
    qapp.processEvents()  # the GUI loop is still responsive right after the click


def test_resume_run_now_and_delete_are_wired(qapp: QApplication) -> None:
    tasks = FakeDirectory([_handle("m1", state="paused")])
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    _click(_button(dash, "resume", "m1"))
    _click(_button(dash, "run_now", "m1"))
    _click(_button(dash, "delete", "m1"))  # delete == §3.12 cancel (retires the task)
    assert tasks.calls == [("m1", "resume"), ("m1", "run_now"), ("m1", "cancel")]


def test_buttons_follow_state(qapp: QApplication) -> None:
    tasks = FakeDirectory(
        [
            _handle("m1", state="waiting"),
            _handle("m2", instruction="second", state="paused"),
            _handle("m3", instruction="third", state="completed"),
        ]
    )
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    assert _button(dash, "pause", "m1").isEnabled()
    assert not _button(dash, "resume", "m1").isEnabled()
    assert not _button(dash, "pause", "m2").isEnabled()  # already paused
    assert _button(dash, "resume", "m2").isEnabled()
    assert not _button(dash, "pause", "m3").isEnabled()  # terminal
    assert not _button(dash, "run_now", "m3").isEnabled()
    assert _button(dash, "delete", "m3").isEnabled()


def test_table_refreshes_from_the_directory(qapp: QApplication) -> None:
    tasks = FakeDirectory([_handle("m1", state="waiting")])
    dash = TasksDashboard(bus=EventBus(), tasks=tasks)
    dash.show_dashboard()
    tasks.tasks[0].state = "paused"
    _pump(qapp, lambda: dash.table.item(0, 2).text() == "paused", timeout_s=4.0)


# -- the live trace pane -----------------------------------------------------------


def test_task_events_from_engine_threads_update_trace(qapp: QApplication) -> None:
    bus = EventBus()
    dash = TasksDashboard(bus=bus, tasks=FakeDirectory())
    # The engine emits from non-GUI threads (monitor loop, planner workers) via
    # emit_threadsafe → the bus drain thread; widgets are only touched on the
    # GUI thread through the queued signal.
    bus.emit_threadsafe(TaskEvent(task_id="m1", type="waiting", payload="check 1 errored"))
    bus.emit_threadsafe(TaskEvent(task_id="m1", type="result", payload="folder changed"))
    bus.emit_threadsafe(TaskEvent(task_id="f1", type="step", payload="clicked File menu"))
    _pump(qapp, lambda: "check 1 errored" in dash.trace.toPlainText())
    text = dash.trace.toPlainText()
    assert "folder changed" in text  # monitor result history (§3.12)
    assert "clicked File menu" in text  # foreground steps in the same pane
    assert "[m1]" in text and "[f1]" in text
    bus.stop()


def test_task_event_emitted_on_gui_thread_updates_trace(qapp: QApplication) -> None:
    bus = EventBus()
    dash = TasksDashboard(bus=bus, tasks=FakeDirectory())
    bus.emit(TaskEvent(task_id="m1", type="started"))  # sync dispatch on this thread
    _pump(qapp, lambda: "started" in dash.trace.toPlainText())
    bus.stop()


# -- show_dashboard ----------------------------------------------------------------


def test_show_dashboard_raises_and_shows(qapp: QApplication) -> None:
    dash = TasksDashboard(bus=EventBus(), tasks=FakeDirectory([_handle("m1")]))
    assert not dash.isVisible()
    dash.show_dashboard()
    assert dash.isVisible()
    dash.close()  # dismissed — but summoned, not resident (§3.11)
    assert not dash.isVisible()
    dash.show_dashboard()  # a second summon works
    assert dash.isVisible()


# -- App wiring: tray open_ui → GUI-thread marshal → lazy dashboard ----------------


class _HeadlessHook:
    def install(self, on_key: object) -> int:
        return 0

    def run(self) -> None:
        return None

    def request_stop(self) -> None:
        return None

    def uninstall(self, handle: int) -> None:
        return None


class _SilentAudio:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _IdlePerception:
    def snapshot(
        self, hint: object = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        return PerceptionSnapshot()


class _RefusingActionLayer:
    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult:
        raise AssertionError("the dashboard test must not act on the screen")


class _PassGate:
    def guard(self, execute: Callable[[ActionRequest], ActionResult]) -> Callable[
        [ActionRequest], ActionResult
    ]:
        return execute


class _SilentAsk:
    def ask(
        self, question: str, cancelled: Callable[[], bool] | None = None
    ) -> str:
        return ""


def _gui_app(tmp_path: Path) -> App:
    """A GUI-mode App on injected fakes (no mic, no screen, no store writes)."""
    return App(
        Settings(activation="hotkey"),
        headless=False,
        log_dir=tmp_path,
        stt=FakeSTT(),
        audio_engine=_SilentAudio(),  # type: ignore[arg-type]
        hotkey_backend=_HeadlessHook(),  # type: ignore[arg-type]
        perception=_IdlePerception(),  # type: ignore[arg-type]
        action_layer=_RefusingActionLayer(),  # type: ignore[arg-type]
        gate=_PassGate(),  # type: ignore[arg-type]
        ask_channel=_SilentAsk(),  # type: ignore[arg-type]
        planner=FakePlanner(),
        task_store_path=tmp_path / "tasks.json",
    )


def test_app_open_ui_marshals_dashboard_onto_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    app = _gui_app(tmp_path)
    try:
        assert app.dashboard is None  # lazy: nothing built until open_ui arrives
        # The tray menu emits exactly this (ui/tray.py), and the handler runs on
        # the bus drain thread — the dashboard must appear via the GUI marshal.
        app.bus.emit_threadsafe(TrayAction(kind="open_ui"))
        _pump(qapp, lambda: app.dashboard is not None and app.dashboard.isVisible())
        assert app.dashboard.isVisible()
        # App satisfies TaskDirectory — the dashboard reads the real manager.
        assert app.list_tasks() == app.manager.list_tasks()
        assert app.control("unknown-id", "pause") is False
    finally:
        app.stop()


def test_app_task_directory_delegates_to_manager(
    qapp: QApplication, tmp_path: Path
) -> None:
    app = _gui_app(tmp_path)
    try:
        assert app.list_tasks() == []
        # register() starts the resident loop itself; the loop checks C:/x
        # (nonexistent) once — a first check captures the baseline, no trigger.
        handle = app.manager.register(
            TaskSpec(
                id="m9",
                instruction="watch x",
                mode="background_monitor",
                monitor=MonitorSpec(
                    kind="file_hash", target="C:/x", interval_s=60, condition="changed"
                ),
            )
        )
        assert [h.spec.id for h in app.list_tasks()] == ["m9"]  # mirrors the manager
        assert app.list_tasks("waiting") == []  # the state filter passes through
        # The App seam reaches the real manager: pause lands on the handle.
        assert app.control("m9", "pause") is True
        assert handle.state == "paused"
        assert [h.spec.id for h in app.list_tasks("paused")] == ["m9"]
        assert app.control("unknown-id", "pause") is False
    finally:
        app.stop()
