"""Mascot + tray tests run fully offscreen (QT_QPA_PLATFORM set in conftest)."""

import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from marshmellow.core.bus import EventBus  # noqa: E402
from marshmellow.ui.mascot import MASCOT_STATES, Mascot, MascotClicked  # noqa: E402
from marshmellow.ui.tray import Tray, TrayAction  # noqa: E402


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
