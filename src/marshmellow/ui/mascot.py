"""Mascot overlay — the always-on-top ambient presence (01-architecture §3.10).

Placeholder art: a rounded marshmallow whose fill color encodes agent state
(real art pipeline per OQ-3). Idle is click-through so it never blocks the
screen; any active state accepts clicks (which publish MascotClicked).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from marshmellow.core.bus import EventBus

MascotState = Literal[
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted"
]
MASCOT_STATES: tuple[MascotState, ...] = (
    "idle", "listening", "thinking", "acting", "speaking", "error", "muted",
)

_STATE_COLORS: dict[MascotState, str] = {
    "idle": "#e8e8e8",
    "listening": "#7fd4ff",
    "thinking": "#c9a7ff",
    "acting": "#ffd47f",
    "speaking": "#9fe8b8",
    "error": "#ff8f8f",
    "muted": "#9a9a9a",
}


class MascotClicked(BaseModel):
    pass


class Mascot(QWidget):
    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__()
        self._bus = bus
        self._state: MascotState = "idle"
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(64, 64)
        self._apply_click_through()

    @property
    def state(self) -> MascotState:
        return self._state

    def set_state(self, state: MascotState) -> None:
        if state not in _STATE_COLORS:
            raise ValueError(f"unknown mascot state: {state!r}")
        self._state = state
        self._apply_click_through()
        self.update()

    def _apply_click_through(self) -> None:
        flag = Qt.WindowType.WindowTransparentForInput
        self.setWindowFlag(flag, self._state == "idle")

    def mousePressEvent(self, event: object) -> None:
        if self._bus is not None:
            self._bus.emit_threadsafe(MascotClicked())
        event.accept()  # type: ignore[attr-defined]

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_STATE_COLORS[self._state]))
        painter.drawRoundedRect(self.rect().adjusted(6, 6, -6, -6), 18, 18)
        painter.end()
