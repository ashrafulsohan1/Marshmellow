"""Tasks dashboard — the main window (01-architecture §3.11, M4 Task 6).

Summoned, not resident: the tray's ``open_ui`` action arrives on the bus drain
thread (the tray emits ``emit_threadsafe``), so composition marshals it onto
the GUI thread (``GuiMarshal`` — the non-blocking cousin of tray.py's
``GuiMarshaledConfirm``) and lazily builds ONE ``TasksDashboard``. The
dashboard is a thin EventBus subscriber — the engine never depends on it
running (headless mode never builds one).

Threading contract: TaskEvents are emitted from engine threads (monitor loops
on the resident asyncio thread, planner workers); the bus handler re-emits
them over a QUEUED Qt Signal, so only the GUI thread ever touches widgets.
Row controls call ``TaskDirectory.control`` — TaskManager.control's documented
callers include the GUI thread (it marshals onto the resident loop and returns
False for commands that do not apply) — then refresh the table from
``list_tasks()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import TaskEvent, TaskState

if TYPE_CHECKING:
    from marshmellow.tasks.manager import TaskHandle

TaskCommand = Literal["pause", "resume", "cancel", "run_now"]


class TaskDirectory(Protocol):
    """The task seam the dashboard reads and controls (App and TaskManager
    both satisfy it — the UI depends on this protocol, never on the engine)."""

    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]: ...

    def control(self, task_id: str, cmd: TaskCommand) -> bool: ...


#: Row buttons: objectName prefix → label. ``delete`` maps to §3.12
#: ``control("cancel")`` — deleting a persistent background task retires it
#: (the manager drops its persisted row eagerly at the op).
_BUTTONS: tuple[tuple[TaskCommand, str], ...] = (
    ("pause", "Pause"),
    ("resume", "Resume"),
    ("run_now", "Run now"),
    ("delete", "Delete"),
)

#: Live states — where control() says pause/resume/run_now apply (mirrors
#: TaskManager._control; terminal states only allow Delete).
_LIVE_STATES: frozenset[str] = frozenset({"pending", "waiting", "running", "paused"})

#: Button key → §3.12 control op (delete == cancel: a deleted persistent task
#: is retired — the manager drops its persisted row eagerly at the op).
_CONTROL_OF: dict[str, TaskCommand] = {
    "pause": "pause",
    "resume": "resume",
    "run_now": "run_now",
    "delete": "cancel",
}

_COLS: tuple[str, ...] = (
    "ID",
    "Instruction",
    "State",
    "Last result / error",
    "Next check",
    "Actions",
)
_ACTIONS_COL = len(_COLS) - 1

#: §3.11 "summoned, not resident" still shows LIVE state: the table re-reads
#: the directory on this cadence while the window is visible.
_REFRESH_MS = 2000

#: §3.12 "all monitor history in dashboard" — QPlainTextEdit trims the OLDEST
#: lines past this cap, so the pane holds the whole recent session's trace with
#: bounded memory.
_TRACE_MAX_BLOCKS = 2000


class GuiMarshal(QObject):
    """Fire-and-forget GUI-thread marshal (the ``GuiMarshaledConfirm`` pattern).

    ``open_ui`` arrives from the bus drain thread; widgets must be built and
    shown on the GUI thread, and there is nothing to wait for — so unlike the
    confirm marshal this is a QUEUED, non-blocking hop (the bus thread never
    waits behind a window). Called on the GUI thread itself it degrades to a
    direct call (a same-thread queued emit would only run later anyway).
    """

    _requested = Signal()

    def __init__(self, run: Callable[[], None]) -> None:
        super().__init__()
        self._run = run
        self._requested.connect(self._fire, Qt.ConnectionType.QueuedConnection)

    def request(self) -> None:
        if QThread.currentThread() is self.thread():  # already the GUI thread
            self._fire()
        else:
            self._requested.emit()

    def _fire(self) -> None:
        self._run()


class TasksDashboard(QMainWindow):
    """§3.11 tasks dashboard: the background-task table + the live trace pane."""

    _event_received = Signal(object)  # carries a TaskEvent; queued across threads

    def __init__(self, bus: EventBus, tasks: TaskDirectory) -> None:
        super().__init__()
        self._bus = bus
        self._tasks = tasks
        self.setObjectName("tasks-dashboard")
        self.setWindowTitle("Marshmellow — Tasks")

        self.table = QTableWidget(0, len(_COLS), self)
        self.table.setHorizontalHeaderLabels(list(_COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        self.trace = QPlainTextEdit(readOnly=True)
        self.trace.setMaximumBlockCount(_TRACE_MAX_BLOCKS)
        self.trace.setPlaceholderText(
            "Task trace — foreground steps and monitor history (§3.12)"
        )

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self.table, 3)
        layout.addWidget(QLabel("Trace", central))
        layout.addWidget(self.trace, 2)
        self.setCentralWidget(central)
        self.resize(900, 600)

        # Engine threads → queued Signal → GUI thread. The bus handler below
        # runs on whatever thread emitted the event; the slot runs on ours.
        self._event_received.connect(self._append_trace, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe = bus.subscribe(TaskEvent, self._on_task_event)

        # Live table: refresh on the cadence while visible (and once per summon).
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_if_visible)
        self._refresh_timer.start()
        self._refresh()

    def show_dashboard(self) -> None:
        """Summon (or re-summon) the window: fresh table, shown, on top."""
        self._refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    # -- bus seam (engine threads) ---------------------------------------------

    def _on_task_event(self, event: TaskEvent) -> None:
        """Bus handler — runs on engine/bus threads; NEVER touches widgets.

        The queued signal is the hop: the event is appended on the GUI thread
        (and the hidden dashboard still collects history between summons, so
        the pane shows everything that happened, §3.12).
        """
        self._event_received.emit(event)

    # -- table ------------------------------------------------------------------

    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self._refresh()

    def _refresh(self) -> None:
        tasks = self._tasks.list_tasks()
        self.table.setRowCount(len(tasks))
        for row, handle in enumerate(tasks):
            self.table.setItem(row, 0, QTableWidgetItem(handle.spec.id))
            self.table.setItem(row, 1, QTableWidgetItem(handle.spec.instruction))
            self.table.setItem(row, 2, QTableWidgetItem(handle.state))
            self.table.setItem(row, 3, QTableWidgetItem(_last_report(handle)))
            self.table.setItem(row, 4, QTableWidgetItem(_next_check(handle)))
            self.table.setCellWidget(row, _ACTIONS_COL, self._row_buttons(handle))

    def _row_buttons(self, handle: TaskHandle) -> QWidget:
        bar = QWidget(self.table)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)
        state = handle.state
        for cmd, label in _BUTTONS:
            button = QPushButton(label, bar)
            button.setObjectName(f"{cmd}:{handle.spec.id}")
            if cmd == "pause":
                button.setEnabled(state in _LIVE_STATES and state != "paused")
            elif cmd == "resume":
                button.setEnabled(state == "paused")
            elif cmd == "run_now":
                button.setEnabled(state in _LIVE_STATES)
            # delete stays enabled for every state — cancel retires anything.
            button.clicked.connect(
                lambda checked=False, task_id=handle.spec.id, op=_CONTROL_OF[cmd]: self._control(
                    op, task_id
                )
            )
            layout.addWidget(button)
        return bar

    def _control(self, cmd: TaskCommand, task_id: str) -> None:
        """One row control op, then re-read the directory (the state moved).

        ``TaskManager.control`` is thread-safe for GUI-thread callers (it
        marshals onto the resident loop, ≤5 s) and returns False for commands
        that do not apply — a stale row's click is a no-op, never an error.
        """
        self._tasks.control(task_id, cmd)
        self._refresh()

    # -- trace pane (GUI thread) -------------------------------------------------

    def _append_trace(self, event: TaskEvent) -> None:
        line = f"{event.at:%H:%M:%S}  [{event.task_id}] {event.type}"
        if event.payload:
            line += f" — {event.payload}"
        self.trace.appendPlainText(line)


def _last_report(handle: TaskHandle) -> str:
    """The last result/error column: the error if one stands, else the spoken
    summary of the last result (§3.11 "last result, next check")."""
    if handle.error:
        return f"error: {handle.error}"
    if handle.result is not None:
        return handle.result.summary_to_speak
    return "—"


def _next_check(handle: TaskHandle) -> str:
    due = handle.next_due if handle.next_due is not None else handle.spec.schedule
    return due.strftime("%Y-%m-%d %H:%M") if due is not None else "—"
