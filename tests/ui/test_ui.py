"""Mascot + tray tests run fully offscreen (QT_QPA_PLATFORM set in conftest)."""

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QThread, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.ui import tray as tray_module  # noqa: E402
from marshmellow.ui.mascot import (  # noqa: E402
    _STATE_COLORS,
    MASCOT_STATES,
    Mascot,
    MascotClicked,
)
from marshmellow.ui.tray import (  # noqa: E402
    GuiMarshaledConfirm,
    Tray,
    TrayAction,
    confirm_dialog,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_mascot_accepts_all_states(qapp: QApplication) -> None:
    m = Mascot()
    for state in MASCOT_STATES:
        m.set_state(state)
        assert m.state == state


def test_mascot_idle_is_click_through(qapp: QApplication) -> None:
    m = Mascot()  # never shown: windowFlags() is the source of truth, not windowHandle()
    m.set_state("idle")
    assert bool(m.windowFlags() & Qt.WindowType.WindowTransparentForInput)
    m.set_state("listening")
    assert not bool(m.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def test_mascot_attention_state_is_the_foreground_lease_ask(qapp: QApplication) -> None:
    """D6 (§3.12): a background task asking for the screen shows "attention"
    (#f6a5c0) while the consent question is up — an active (clickable) state."""
    assert "attention" in MASCOT_STATES
    assert _STATE_COLORS["attention"] == "#f6a5c0"
    m = Mascot()
    m.set_state("attention")
    assert m.state == "attention"
    assert not bool(m.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def test_mascot_click_publishes_event(qapp: QApplication) -> None:
    bus = EventBus()
    done = threading.Event()
    bus.subscribe(MascotClicked, lambda ev: done.set())
    m = Mascot(bus=bus)
    m.set_state("acting")  # interactive state
    m.show()
    QTest.mouseClick(m, Qt.MouseButton.LeftButton)
    assert done.wait(timeout=5.0)
    bus.stop()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Condition-bounded wait for cross-thread delivery (never a fixed sleep)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_tray_menu_actions_publish(qapp: QApplication) -> None:
    bus = EventBus()
    kinds: list[str] = []

    bus.subscribe(TrayAction, lambda ev: kinds.append(ev.kind))
    tray = Tray(bus=bus)
    tray.action("mute").trigger()
    assert _wait_for(lambda: kinds == ["mute"])
    tray.action("quit").trigger()
    assert _wait_for(lambda: kinds == ["mute", "quit"])
    bus.stop()


def test_mascot_set_state_from_engine_thread_is_queued(qapp: QApplication) -> None:
    """Engine threads (bus drain, voice loop) must never mutate the widget
    directly: set_state lands on the GUI thread via a queued connection."""
    import time

    m = Mascot()
    t = threading.Thread(target=lambda: m.set_state("acting"))
    t.start()
    t.join()

    deadline = time.monotonic() + 5.0
    while m.state != "acting" and time.monotonic() < deadline:
        qapp.processEvents()
    assert m.state == "acting"


def _close_modal_later(button: QMessageBox.StandardButton, timeout_ms: int = 2000) -> None:
    """Answer the next active modal dialog with `button` via the Qt event loop
    (no threads, no fixed sleeps: a QTimer polls until the dialog is up).
    Parented to the app so the timer survives the local scope while exec() runs."""
    timer = QTimer(QApplication.instance())

    def try_close() -> None:
        widget = QApplication.activeModalWidget()
        if widget is not None:
            widget.done(int(button))
            timer.stop()

    timer.timeout.connect(try_close)
    timer.start(10)
    QTimer.singleShot(timeout_ms, timer.stop)


def test_confirm_dialog_accept_returns_true(qapp: QApplication) -> None:
    _close_modal_later(QMessageBox.StandardButton.Yes)
    assert confirm_dialog("Delete 6 items — confirm?") is True


def test_confirm_dialog_dismiss_returns_false(qapp: QApplication) -> None:
    _close_modal_later(QMessageBox.StandardButton.No)
    assert confirm_dialog("Delete 6 items — confirm?") is False


def test_confirm_dialog_default_button_is_no(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.13 fail closed: the dialog's default must be No (Enter dismisses)."""
    defaults: list[str] = []

    def capture_exec(self: QMessageBox) -> int:
        button = self.defaultButton()
        defaults.append(button.text() if button is not None else "")
        return int(QMessageBox.StandardButton.No)

    monkeypatch.setattr(QMessageBox, "exec", capture_exec)
    assert confirm_dialog("Delete 6 items — confirm?") is False
    assert defaults == ["&No"]


# -- GuiMarshaledConfirm: the §3.13 fallback crosses threads safely -------------


def _spin_until(t: threading.Thread, qapp: QApplication, timeout_s: float = 5.0) -> None:
    """Wait for a worker thread while THIS (GUI) thread services the event
    loop — exactly what the real GUI thread does while a queued dialog shows."""
    deadline = time.monotonic() + timeout_s
    while t.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
    t.join(timeout=timeout_s)


def test_gui_marshaled_confirm_executes_on_the_gui_thread(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate calls the fallback from a planner worker thread; the dialog
    callable must run on the GUI thread (composition marshal, §3.13)."""
    main_thread = QThread.currentThread()
    seen: list[QThread] = []

    def fake_dialog(question: str) -> bool:
        seen.append(QThread.currentThread())
        return True

    monkeypatch.setattr(tray_module, "confirm_dialog", fake_dialog)
    marshal = GuiMarshaledConfirm()

    holder: list[bool] = []
    worker = threading.Thread(target=lambda: holder.append(marshal("confirm?")))
    worker.start()
    _spin_until(worker, qapp)

    assert holder == [True]  # the worker got its answer back
    assert seen == [main_thread]  # …and the dialog ran on the GUI thread, not the worker's


def test_gui_marshaled_confirm_shows_the_real_dialog_from_a_worker(
    qapp: QApplication,
) -> None:
    """Full fidelity: the REAL QMessageBox is created and answered end to end
    from a worker thread call — no cross-thread widget crash, fail-closed No."""
    marshal = GuiMarshaledConfirm()
    _close_modal_later(QMessageBox.StandardButton.No)

    holder: list[bool] = []
    worker = threading.Thread(target=lambda: holder.append(marshal("Delete 6 items — confirm?")))
    worker.start()
    _spin_until(worker, qapp)

    assert holder == [False]
    assert QApplication.activeModalWidget() is None  # shown, answered, gone


def test_gui_marshaled_confirm_on_the_gui_thread_is_direct(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-thread blocking-queued emit would deadlock: on the GUI thread the
    marshal must call through directly."""
    seen: list[QThread] = []

    def fake_dialog(question: str) -> bool:
        seen.append(QThread.currentThread())
        return True

    monkeypatch.setattr(tray_module, "confirm_dialog", fake_dialog)
    marshal = GuiMarshaledConfirm()

    assert marshal("confirm?") is True  # returns at all — no deadlock
    assert seen == [QThread.currentThread()]
