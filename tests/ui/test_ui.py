"""Mascot + tray tests run fully offscreen (QT_QPA_PLATFORM set in conftest)."""

import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.ui.mascot import MASCOT_STATES, Mascot, MascotClicked  # noqa: E402
from marshmellow.ui.tray import Tray, TrayAction, confirm_dialog  # noqa: E402


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
