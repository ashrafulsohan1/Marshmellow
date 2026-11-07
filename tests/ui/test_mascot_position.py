"""Mascot placement — bottom-right default, drag, persistence, clamp (M6-T2).

All offscreen (QT_QPA_PLATFORM=offscreen in conftest; one 800×800 screen with
availableGeometry == geometry). The OS-level input pass-through of a window
mask cannot be exercised offscreen — QTest posts events straight to the widget
— so the "never blocks the screen" promise (§3.10) is pinned via the window
MASK itself (exactly the region the window system maps input through): the
body ellipse center is interactive, the corners around it are not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import marshmellow.app as app_module  # noqa: E402
from marshmellow.app import App  # noqa: E402
from marshmellow.core.config import Settings, load_settings  # noqa: E402
from marshmellow.ui.mascot import (  # noqa: E402
    _EDGE_MARGIN,
    MASCOT_STATES,
    Mascot,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _primary_available() -> QRect:
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    return screen.availableGeometry()


# -- default placement ----------------------------------------------------------


def test_default_position_is_bottom_right(qapp: QApplication) -> None:
    m = Mascot()
    geo = _primary_available()
    g = m.geometry()
    assert geo.contains(g)  # fully on the primary screen's available area
    assert g.center().x() > geo.center().x()  # right half…
    assert g.center().y() > geo.center().y()  # …bottom half…
    assert geo.right() - g.right() <= _EDGE_MARGIN  # …hugging the tray corner
    assert geo.bottom() - g.bottom() <= _EDGE_MARGIN


# -- drag + persistence ---------------------------------------------------------


def test_drag_emits_on_position_changed(qapp: QApplication) -> None:
    seen: list[tuple[int, int]] = []
    m = Mascot(on_position_changed=lambda x, y: seen.append((x, y)))
    m.show()
    start = m.pos()
    QTest.mousePress(m, Qt.MouseButton.LeftButton, pos=QPoint(32, 32))
    QTest.mouseMove(m, QPoint(40, 40))
    QTest.mouseMove(m, QPoint(52, 52))
    QTest.mouseRelease(m, Qt.MouseButton.LeftButton, pos=QPoint(52, 52))

    assert seen, "drag end must report the position"
    assert seen[-1] == (m.x(), m.y())  # the reported position is the widget's
    assert m.pos().x() > start.x() and m.pos().y() > start.y()  # it really moved


def test_click_without_move_does_not_save(qapp: QApplication) -> None:
    seen: list[tuple[int, int]] = []
    m = Mascot(on_position_changed=lambda x, y: seen.append((x, y)))
    m.show()
    QTest.mouseClick(m, Qt.MouseButton.LeftButton)
    assert seen == []  # a click is MascotClicked, not a position change


def test_persisted_position_restores(qapp: QApplication) -> None:
    m = Mascot(initial_pos=QPoint(100, 150))
    assert m.pos() == QPoint(100, 150)


def test_dragged_position_round_trips(qapp: QApplication) -> None:
    """Drag → the saved (x, y) fed back as initial_pos restores the same spot."""
    seen: list[tuple[int, int]] = []
    m = Mascot(initial_pos=QPoint(200, 200), on_position_changed=lambda x, y: seen.append((x, y)))
    m.show()
    QTest.mousePress(m, Qt.MouseButton.LeftButton, pos=QPoint(32, 32))
    QTest.mouseMove(m, QPoint(50, 60))
    QTest.mouseRelease(m, Qt.MouseButton.LeftButton, pos=QPoint(50, 60))

    assert seen
    restored = Mascot(initial_pos=QPoint(*seen[-1]))
    assert restored.pos() == QPoint(*seen[-1])


# -- clamp (the unplugged-monitor case) ------------------------------------------


def test_out_of_restore_clamps_into_visible_screen(qapp: QApplication) -> None:
    """A position saved on a monitor that no longer exists must not strand the
    mascot off-screen: (50000, 50000) lands on a visible screen's geometry."""
    m = Mascot(initial_pos=QPoint(50000, 50000))
    geo = _primary_available()
    assert geo.contains(m.geometry())


def test_drag_beyond_the_edge_stays_on_screen(qapp: QApplication) -> None:
    m = Mascot(initial_pos=QPoint(100, 100))
    m.show()
    QTest.mousePress(m, Qt.MouseButton.LeftButton, pos=QPoint(32, 32))
    QTest.mouseMove(m, QPoint(32, 32 - 10000))  # drag far past the top edge
    QTest.mouseRelease(m, Qt.MouseButton.LeftButton, pos=QPoint(32, 32 - 10000))
    geo = _primary_available()
    assert geo.contains(m.geometry())


# -- body-masked input (the §3.10 never-blocks promise) ---------------------------


def test_body_mask_is_nonempty_and_smaller_than_the_widget(qapp: QApplication) -> None:
    m = Mascot()
    mask = m.mask()
    assert not mask.isEmpty()
    bounds = mask.boundingRect()
    assert 0 < bounds.width() < m.width()
    assert 0 < bounds.height() < m.height()
    assert mask.contains(m.rect().center())  # the body is clickable…
    for corner in (QPoint(0, 0), QPoint(m.width() - 1, 0), QPoint(0, m.height() - 1)):
        assert not mask.contains(corner)  # …its surroundings pass through


def test_body_masked_input_in_every_state(qapp: QApplication) -> None:
    """The mask model replaces WindowTransparentForInput: no state ever sets
    the whole-rect input-off flag, and every state keeps an interactive body."""
    m = Mascot()
    for state in MASCOT_STATES:
        m.set_state(state)
        assert not bool(m.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        assert not m.mask().isEmpty()
        assert m.mask().contains(m.rect().center())


# -- animation clock: 30 fps ONLY while the state animates ------------------------


def test_idle_renders_one_static_frame_with_zero_timers(qapp: QApplication) -> None:
    m = Mascot()
    m.show()
    m.set_state("idle")
    assert not m._anim_timer.isActive()
    m.set_state("muted")  # the flat-line state never animates either
    assert not m._anim_timer.isActive()


def test_animating_state_runs_a_30fps_timer(qapp: QApplication) -> None:
    m = Mascot()
    m.show()
    m.set_state("speaking")
    assert m._anim_timer.isActive()
    assert m._anim_timer.interval() == 33  # 1000 ms / 30 fps
    m.set_state("idle")  # back to the static frame, timer stops
    assert not m._anim_timer.isActive()


# -- App wiring: persisted position in, drag-end save out (headless) --------------


def _headless_app(tmp_path: Path, settings: Settings) -> App:
    return App(
        settings=settings,
        headless=True,
        log_dir=tmp_path,
        task_store_path=tmp_path / "tasks.json",
    )


def test_app_passes_persisted_position_on_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings()
    app = _headless_app(tmp_path, settings)
    assert app._mascot_initial_pos() is None  # unset → widget's bottom-right default

    settings.ui.mascot_x = 50
    settings.ui.mascot_y = 60
    pos = app._mascot_initial_pos()
    assert pos is not None and pos.x() == 50 and pos.y() == 60


def test_app_saves_position_on_drag_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(app_module, "product_config_path", lambda: cfg)
    app = _headless_app(tmp_path, Settings())

    app._on_mascot_moved(123, 456)
    loaded = load_settings(cfg)
    assert loaded.ui.mascot_x == 123
    assert loaded.ui.mascot_y == 456
