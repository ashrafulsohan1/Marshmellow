"""Mascot overlay — the always-on-top ambient presence (01-architecture §3.10).

M6-T2: the widget paints the M6-T1 parametric painter (`ui.mascot_art`) and
places itself at the bottom-right of the primary screen's available geometry
by default; the position is draggable and `App` persists it across restarts
(`[ui] mascot_x / mascot_y` in config.toml, saved on drag end, clamped into a
visible screen on restore — the unplugged-monitor case).

Input model — body-masked input: the window's input mask is the marshmallow
body ellipse (`setMask`), REPLACING the old WindowTransparentForInput toggle.
Everything outside the ellipse passes through to the windows below in EVERY
state — the "never blocks the screen" promise — while the body itself stays
draggable and clickable in every state (a press still publishes MascotClicked).

Animation clock: a 30 fps QTimer drives `mascot_art`'s phase ONLY while the
current state animates (nonzero `mascot_art.PERIODS`). Idle renders ONE static
frame with zero timers — the §3.10 budget is 0% CPU idle and the mascot sits in
idle for most of its life. The idle bob stays painter-side (`mascot_art.
PERIODS` keeps it for the export tool; add "idle" to `_animated_states` to
re-enable the on-screen bob). "muted" is the flat-line state, never animates.

`set_state` is called from engine threads (bus drain, voice loop, task
workers) — it emits a Qt signal that lands on the GUI thread via a queued
connection, so no widget mutation ever happens off the Qt main thread.
"attention" is the §3.12 D6 foreground-lease ask: a background task wants the
screen and is waiting for the user's consent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel
from PySide6.QtCore import QElapsedTimer, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QMouseEvent, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from marshmellow.core.bus import EventBus

MascotState = Literal[
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted", "attention"
]
MASCOT_STATES: tuple[MascotState, ...] = (
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted", "attention",
)

_STATE_COLORS: dict[MascotState, str] = {
    "idle": "#e8e8e8",
    "listening": "#7fd4ff",
    "thinking": "#c9a7ff",
    "acting": "#ffd47f",
    "speaking": "#9fe8b8",
    "error": "#ff8f8f",
    "muted": "#9a9a9a",
    "attention": "#f6a5c0",  # foreground-lease ask (§3.12 D6): consent wanted
}

MASCOT_SIZE = 64
_FPS = 30
_EDGE_MARGIN = 12  # px between the mascot and the available-geometry edges


class MascotClicked(BaseModel):
    pass


class MascotStateEvent(BaseModel):
    """The mascot changed state (M6-T5): the tray mirrors it in its identity
    icon (idle/muted/attention must read differently at a glance)."""

    state: MascotState


def _animated_states() -> frozenset[str]:
    """States the widget animates with the 30 fps clock. Lazy import: ui.
    mascot_art imports this module's color constants, so the reverse import
    must not run at module import time. Idle is deliberately static (see
    module docstring); add "idle" here to re-enable the on-screen bob."""
    from marshmellow.ui.mascot_art import PERIODS

    return frozenset(s for s in MASCOT_STATES if s != "idle" and PERIODS[s] > 0.0)


class Mascot(QWidget):
    _state_requested = Signal(str)

    def __init__(
        self,
        bus: EventBus | None = None,
        initial_pos: QPoint | None = None,
        on_position_changed: Callable[[int, int], None] | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._state: MascotState = "idle"
        self._on_position_changed = on_position_changed
        self._phase = 0.0
        self._clock = QElapsedTimer()
        self._drag_offset: QPoint | None = None
        self._drag_moved = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(MASCOT_SIZE, MASCOT_SIZE)
        self._apply_body_mask()
        if initial_pos is not None:
            self.move(self._clamp_point(initial_pos))  # restore clamps into view
        else:
            self.move(self._default_position())
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(1000 // _FPS)
        self._anim_timer.timeout.connect(self._tick)
        self._state_requested.connect(self._apply_state)  # queued from other threads

    @property
    def state(self) -> MascotState:
        return self._state

    def set_state(self, state: MascotState) -> None:
        """Thread-safe: on the GUI thread this is direct, elsewhere queued."""
        self._state_requested.emit(state)

    # -- placement ----------------------------------------------------------------

    def _default_position(self) -> QPoint:
        """Bottom-right of the primary screen's available geometry (which
        already excludes the taskbar), kept `_EDGE_MARGIN` off the edges."""
        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen is not None else QRect(0, 0, 800, 800)
        return QPoint(
            geo.right() - self.width() - _EDGE_MARGIN + 1,
            geo.bottom() - self.height() - _EDGE_MARGIN + 1,
        )

    def _clamp_point(self, pos: QPoint) -> QPoint:
        """Clamp a would-be top-left so the widget stays fully inside a
        currently-visible screen's available geometry. A point off every live
        screen (restored onto an unplugged monitor) lands on the nearest one."""
        intended = QRect(pos, self.size())
        screen = QGuiApplication.screenAt(intended.center())
        if screen is None:
            screens = QGuiApplication.screens()
            if not screens:
                return pos  # no screens at all: nothing to clamp into
            screen = min(
                screens,
                key=lambda s: (
                    s.availableGeometry().center() - intended.center()
                ).manhattanLength(),
            )
        geo = screen.availableGeometry()
        x = min(max(pos.x(), geo.left()), geo.right() + 1 - self.width())
        y = min(max(pos.y(), geo.top()), geo.bottom() + 1 - self.height())
        return QPoint(x, y)

    # -- drag (press → track → release saves via on_position_changed) --------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._bus is not None:
            self._bus.emit_threadsafe(MascotClicked())
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.position().toPoint()
            self._drag_moved = False
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._drag_moved = True
            target = event.globalPosition().toPoint() - self._drag_offset
            self.move(self._clamp_point(target))  # live clamp: never leaves the screen
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            moved = self._drag_moved
            self._drag_moved = False
            if moved and self._on_position_changed is not None:
                self._on_position_changed(self.x(), self.y())  # drag end → App persists
        event.accept()

    # -- state + animation clock ----------------------------------------------------

    def _apply_state(self, state: str) -> None:
        if state not in _STATE_COLORS:
            raise ValueError(f"unknown mascot state: {state!r}")
        self._state = state  # type: ignore[assignment]
        if self._bus is not None:
            # M6-T5: the tray mirrors the state in its icon. This slot runs on
            # the GUI thread (set_state queues here), but callers live on
            # engine threads — emit_threadsafe keeps the publish thread-safe.
            self._bus.emit_threadsafe(MascotStateEvent(state=state))
        self._sync_animation()
        self.update()

    def _sync_animation(self) -> None:
        """Run the 30 fps timer ONLY while the current state animates; any
        other state stops it and renders one static phase-0 frame."""
        if self._state in _animated_states():
            self._clock.restart()
            self._anim_timer.start()
        else:
            self._anim_timer.stop()
            self._phase = 0.0

    def _tick(self) -> None:
        from marshmellow.ui.mascot_art import PERIODS  # lazy: import cycle

        period = PERIODS[self._state]
        if period <= 0.0:
            self._anim_timer.stop()
            self._phase = 0.0
            return
        self._phase = (self._clock.elapsed() / 1000.0 % period) / period
        self.update()

    # -- input mask + painting --------------------------------------------------------

    def _apply_body_mask(self) -> None:
        """Input mask = the marshmallow body+aura ellipse: outside it every
        click lands on the window below, in EVERY state (§3.10); inside it the
        mascot is draggable and clickable. The mask also clips painting, so it
        hugs the art (body [0.25s..0.75s]×[0.14s..0.74s] + ~0.07s aura)."""
        s = self.width()
        self.setMask(
            QRegion(
                QRect(round(0.18 * s), round(0.06 * s), round(0.64 * s), round(0.76 * s)),
                QRegion.RegionType.Ellipse,
            )
        )

    def paintEvent(self, event: object) -> None:
        from marshmellow.ui.mascot_art import paint  # lazy: import cycle

        painter = QPainter(self)
        paint(painter, self._state, self._phase, self.width())
        painter.end()
